#!/usr/bin/env python3
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pandas as pd
from dotenv import find_dotenv, load_dotenv


load_dotenv(find_dotenv())

MAX_LOAD = float(os.environ["MAX_LOAD"])
MAX_RUS = int(os.environ["MAX_RUS"])
OUT_DIR = Path(os.environ["OUT_DIR"])
Filename = os.environ["Filename"]
MAX_FIBER_DISTANCE_KM = float(os.environ["MAX_FIBER_DISTANCE_KM"])

EPS = 1e-9


@dataclass(frozen=True)
class GreedyOperation:
    kind: Literal["move", "swap"]
    gain: float
    distance_delta: float
    ru_a: int
    du_a: int
    ru_b: int | None = None
    du_b: int | None = None


def build_du_summary(df_cluster: pd.DataFrame) -> pd.DataFrame:
    """
    Retorna a carga, a quantidade de membros e as margens de cada O-DU.

    A quantidade de O-RUs inclui a própria O-DU.
    """
    summary = (
        df_cluster.groupby("O-DU", sort=True)
        .agg(
            CargaAtual=("bandwidth", "sum"),
            QuantidadeRUs=("NumEstacao", "count"),
        )
        .sort_index()
    )

    average_load = summary["CargaAtual"].mean()

    summary["CargaMedia"] = average_load
    summary["MargemCarga"] = MAX_LOAD - summary["CargaAtual"]
    summary["MargemRUs"] = MAX_RUS - summary["QuantidadeRUs"]
    summary["DesvioMedia"] = summary["CargaAtual"] - average_load

    return summary


def _is_better(
    candidate: GreedyOperation,
    best: GreedyOperation | None,
) -> bool:
    """
    Seleciona a operação por:

    1. maior ganho na discrepância;
    2. menor aumento da distância;
    3. ordem determinística dos identificadores.
    """
    if best is None:
        return True

    candidate_key = (
        candidate.gain,
        -candidate.distance_delta,
        1 if candidate.kind == "move" else 0,
        -candidate.ru_a,
        -(candidate.ru_b or 0),
        -candidate.du_a,
        -(candidate.du_b or 0),
    )

    best_key = (
        best.gain,
        -best.distance_delta,
        1 if best.kind == "move" else 0,
        -best.ru_a,
        -(best.ru_b or 0),
        -best.du_a,
        -(best.du_b or 0),
    )

    return candidate_key > best_key


def _find_best_operation(
    df_cluster: pd.DataFrame,
    df_dm: pd.DataFrame,
    fixed_dus: set[int],
) -> GreedyOperation | None:
    """
    Avalia todas as movimentações e trocas viáveis e retorna a operação
    que mais aumenta a soma dos quadrados das cargas das O-DUs.
    """
    loads = (
        df_cluster.groupby("O-DU")["bandwidth"]
        .sum()
        .astype(float)
        .to_dict()
    )

    sizes = (
        df_cluster.groupby("O-DU")
        .size()
        .astype(int)
        .to_dict()
    )

    non_du_rows = (
        df_cluster.loc[
            df_cluster["NumEstacao"] != df_cluster["O-DU"],
            ["NumEstacao", "O-DU", "bandwidth"],
        ]
        .sort_values("NumEstacao")
    )

    rus = [
        (int(ru), int(du), float(bandwidth))
        for ru, du, bandwidth in non_du_rows.itertuples(
            index=False,
            name=None,
        )
    ]

    best = None

    # ------------------------------------------------------------------
    # Movimentações unilaterais
    # ------------------------------------------------------------------
    for ru, source_du, bandwidth in rus:
        source_load = loads[source_du]
        old_distance = float(df_dm.at[ru, source_du])

        for target_du in sorted(fixed_dus):
            if target_du == source_du:
                continue

            if sizes[target_du] >= MAX_RUS:
                continue

            target_load = loads[target_du]

            if target_load + bandwidth > MAX_LOAD:
                continue

            new_distance = float(df_dm.at[ru, target_du])

            if new_distance > MAX_FIBER_DISTANCE_KM:
                continue

            gain = (
                (source_load - bandwidth) ** 2
                + (target_load + bandwidth) ** 2
                - source_load**2
                - target_load**2
            )

            if gain <= EPS:
                continue

            candidate = GreedyOperation(
                kind="move",
                gain=gain,
                distance_delta=new_distance - old_distance,
                ru_a=ru,
                du_a=source_du,
                du_b=target_du,
            )

            if _is_better(candidate, best):
                best = candidate

    # ------------------------------------------------------------------
    # Trocas entre O-RUs de O-DUs diferentes
    # ------------------------------------------------------------------
    for index_a in range(len(rus)):
        ru_a, du_a, bandwidth_a = rus[index_a]

        for index_b in range(index_a + 1, len(rus)):
            ru_b, du_b, bandwidth_b = rus[index_b]

            if du_a == du_b:
                continue

            if bandwidth_a == bandwidth_b:
                continue

            new_distance_a = float(df_dm.at[ru_a, du_b])
            new_distance_b = float(df_dm.at[ru_b, du_a])

            if (
                new_distance_a > MAX_FIBER_DISTANCE_KM
                or new_distance_b > MAX_FIBER_DISTANCE_KM
            ):
                continue

            load_a = loads[du_a]
            load_b = loads[du_b]

            new_load_a = load_a - bandwidth_a + bandwidth_b
            new_load_b = load_b - bandwidth_b + bandwidth_a

            if new_load_a > MAX_LOAD or new_load_b > MAX_LOAD:
                continue

            gain = (
                new_load_a**2
                + new_load_b**2
                - load_a**2
                - load_b**2
            )

            if gain <= EPS:
                continue

            old_distance_a = float(df_dm.at[ru_a, du_a])
            old_distance_b = float(df_dm.at[ru_b, du_b])

            candidate = GreedyOperation(
                kind="swap",
                gain=gain,
                distance_delta=(
                    new_distance_a
                    + new_distance_b
                    - old_distance_a
                    - old_distance_b
                ),
                ru_a=ru_a,
                du_a=du_a,
                ru_b=ru_b,
                du_b=du_b,
            )

            if _is_better(candidate, best):
                best = candidate

    return best


def _apply_operation(
    df_cluster: pd.DataFrame,
    operation: GreedyOperation,
) -> None:
    """Aplica uma movimentação ou troca no DataFrame."""
    if operation.kind == "move":
        df_cluster.loc[
            df_cluster["NumEstacao"] == operation.ru_a,
            "O-DU",
        ] = operation.du_b

    else:
        df_cluster.loc[
            df_cluster["NumEstacao"] == operation.ru_a,
            "O-DU",
        ] = operation.du_b

        df_cluster.loc[
            df_cluster["NumEstacao"] == operation.ru_b,
            "O-DU",
        ] = operation.du_a


def concentrate_load_greedy(
    df_opex: pd.DataFrame,
    df_dm: pd.DataFrame,
    verbose: bool = True,
) -> pd.DataFrame:
    """
    Aumenta diferença de carga entre as O-DUs.

    Parâmetros
    df_opex: DataFrame da solução OPEX/CAPEX.
    df_dm: DataFrame da matriz de distâncias, indexado por NumEstacao.
    verbose

    Retorna: associações estressadas de O-RUS/ODUs
    """
    result = df_opex.copy()

    fixed_dus = set(result["O-DU"].unique())

    initial_summary = build_du_summary(result)
    initial_score = (initial_summary["CargaAtual"] ** 2).sum()

    if verbose:
        print("\n--- Estado inicial das O-DUs ---")
        print(initial_summary.to_string())
        print(f"\nDiferença inicial: {initial_score:.2f}")

    iteration = 0

    while True:
        operation = _find_best_operation(
            result,
            df_dm,
            fixed_dus,
        )

        if operation is None:
            break

        iteration += 1
        _apply_operation(result, operation)

        if verbose:
            if operation.kind == "move":
                print(
                    f"[{iteration:03d}] MOVE: "
                    f"RU {operation.ru_a} | "
                    f"O-DU {operation.du_a} -> {operation.du_b} | "
                    f"ganho={operation.gain:.2f} | "
                    f"delta_distancia={operation.distance_delta:.6f} km"
                )
            else:
                print(
                    f"[{iteration:03d}] SWAP: "
                    f"RU {operation.ru_a} "
                    f"({operation.du_a} -> {operation.du_b}) <-> "
                    f"RU {operation.ru_b} "
                    f"({operation.du_b} -> {operation.du_a}) | "
                    f"ganho={operation.gain:.2f} | "
                    f"delta_distancia={operation.distance_delta:.6f} km"
                )

    final_summary = build_du_summary(result)
    final_score = (final_summary["CargaAtual"] ** 2).sum()

    if verbose:
        print("\n--- Estado final das O-DUs ---")
        print(final_summary.to_string())
        print(f"\nDiferença final: {final_score:.2f}")
        print(f"Ganho total: {final_score - initial_score:.2f}")
        print(f"Operações realizadas: {iteration}")

    return result


def main() -> None:
    #abrindo o arquivo com Associações OPEX/CAPEX feitas pelo ILP
    csv_path = OUT_DIR / f"ilp_{Filename}_opex_capex.csv"
    print(f"Carregando o arquivo {csv_path}")
    df_opex = pd.read_csv(csv_path)
    df_opex["NumEstacao"] = df_opex["NumEstacao"].astype(int)
    df_opex["O-DU"] = df_opex["O-DU"].astype(int)
    
    #abrindo o arquivo resultado da clusterização RU-DU com critério opex_capex
    csv_path = OUT_DIR / f"dm_{Filename}.csv"
    print(f"Carregando o arquivo {csv_path}")
    df_dm = pd.read_csv(csv_path,index_col="NumEstacao")
    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    df_result = concentrate_load_greedy(
        df_opex,
        df_dm
    )

    df_result.to_csv(
        OUT_DIR / f"grd_{Filename}_stress.csv",
        index=False,
    )


if __name__ == "__main__":
    main()
