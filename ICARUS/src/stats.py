import os
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv


load_dotenv(find_dotenv())

Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()


def add_link_distance(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
) -> pd.DataFrame:
    """
    Adiciona a distância RU -> DU de cada associação.
    """
    result = df.copy()
    result["NumEstacao"] = result["NumEstacao"].astype(int)
    result["O-DU"] = result["O-DU"].astype(int)

    result["LinkDistanceKM"] = [
        float(df_dm.at[ru_id, du_id])
        for ru_id, du_id in zip(
            result["NumEstacao"],
            result["O-DU"],
        )
    ]

    return result


def stats(
    df: pd.DataFrame,
    scenario: str,
) -> Dict[str, Any]:
    """
    Calcula e retorna as estatísticas gerais de um cenário.
    O DataFrame deve conter a coluna LinkDistanceKM.
    """
    dist_ru_du = df["LinkDistanceKM"].to_numpy(dtype=float)

    # As associações O-DU -> própria O-DU contribuem com distância zero.
    total_enlace_km = float(dist_ru_du.sum())

    # Exclui as associações locais para média e desvio padrão dos enlaces.
    dist_links = df.loc[
        df["NumEstacao"] != df["O-DU"],
        "LinkDistanceKM",
    ].to_numpy(dtype=float)

    if len(dist_links) == 0:
        media_dist_ru_du = 0.0
        dp_dist_ru_du = 0.0
    elif len(dist_links) == 1:
        media_dist_ru_du = float(dist_links.mean())
        dp_dist_ru_du = 0.0
    else:
        media_dist_ru_du = float(dist_links.mean())
        dp_dist_ru_du = float(dist_links.std(ddof=1))

    qtde_dus = int(df["O-DU"].nunique())

    # Inclui a O-RU co-localizada com a própria O-DU.
    rus_por_du = (
        df.groupby("O-DU")["NumEstacao"]
        .count()
        .astype(int)
    )

    media_qtde_ru_du = float(rus_por_du.mean())
    dp_qtde_ru_du = (
        float(rus_por_du.std(ddof=1))
        if len(rus_por_du) > 1
        else 0.0
    )

    bandwidth_por_du = (
        df.groupby("O-DU")["bandwidth"]
        .sum()
        .astype(float)
    )

    media_bandwidth_du = float(bandwidth_por_du.mean())
    dp_bandwidth_du = (
        float(bandwidth_por_du.std(ddof=1))
        if len(bandwidth_por_du) > 1
        else 0.0
    )

    qtde_pontos = int(len(df))

    print(f"\n--- Estatísticas da Clusterização: {scenario} ---")
    print(f"Quantidade total de pontos (RUs + DUs): {qtde_pontos}")
    print(f"Quantidade de DUs (clusters): {qtde_dus}")
    print(
        f"Média de RUs por DU (cluster): "
        f"{media_qtde_ru_du:.2f} "
        f"(DP: {dp_qtde_ru_du:.2f})"
    )
    print(
        f"Média de Largura de Banda por DU "
        f"(carga total do cluster): "
        f"{media_bandwidth_du:.2f} "
        f"(DP: {dp_bandwidth_du:.2f})"
    )
    print(
        f"Distância total de enlace (soma RU->DU): "
        f"{total_enlace_km:.2f} km"
    )
    print(
        f"Média de distância RU->DU "
        f"(excluindo DUs ligadas a si mesmas): "
        f"{media_dist_ru_du:.2f} km "
        f"(DP: {dp_dist_ru_du:.2f} km)"
    )

    return {
        "Scenario": scenario,
        "TotalPoints": qtde_pontos,
        "NumDUs": qtde_dus,
        "MediaRUsPerDU": media_qtde_ru_du,
        "DP_RUsPerDU": dp_qtde_ru_du,
        "MediaBandwidthPerDU": media_bandwidth_du,
        "DP_BandwidthPerDU": dp_bandwidth_du,
        "TotalLinkDistanceKM": total_enlace_km,
        "MediaLinkDistance": media_dist_ru_du,
        "DP_LinkDistance": dp_dist_ru_du,
    }


def stats_by_odu(
    df: pd.DataFrame,
    scenario: str,
) -> pd.DataFrame:
    """
    Gera as estatísticas por O-DU de um cenário.

    Retorna uma linha por O-DU, contendo:
    - quantidade de O-RUs associadas;
    - soma das distâncias RU -> DU do cluster.
    """
    grouped = (
        df.groupby("O-DU", sort=True)
        .agg(
            NumRUs=("NumEstacao", "count"),
            TotalLinkDistanceKM=("LinkDistanceKM", "sum"),
        )
    )

    grouped["NumRUs"] = grouped["NumRUs"].astype(int)
    grouped["TotalLinkDistanceKM"] = grouped[
        "TotalLinkDistanceKM"
    ].astype(float)

    return grouped.rename(
        columns={
            "NumRUs": f"{scenario}_NumRUs",
            "TotalLinkDistanceKM": (
                f"{scenario}_TotalLinkDistanceKM"
            ),
        }
    )


def build_stats_by_odus(
    tables_by_scenario: Dict[str, pd.DataFrame],
) -> pd.DataFrame:
    """
    Combina horizontalmente os resultados por O-DU de todos os cenários
    e adiciona uma linha final TOTAL.
    """
    if not tables_by_scenario:
        return pd.DataFrame()

    scenarios = list(tables_by_scenario)
    reference_scenario = scenarios[0]
    reference_odus = set(tables_by_scenario[reference_scenario].index)

    # Como as localizações de O-DU devem ser iguais, uma diferença é erro.
    for scenario in scenarios[1:]:
        current_odus = set(tables_by_scenario[scenario].index)

        if current_odus != reference_odus:
            missing = sorted(reference_odus - current_odus)
            extra = sorted(current_odus - reference_odus)
            raise ValueError(
                "Os conjuntos de O-DUs não são iguais entre os cenários. "
                f"Referência: {reference_scenario}; "
                f"cenário divergente: {scenario}; "
                f"ausentes: {missing}; adicionais: {extra}."
            )

    result = pd.concat(
        [tables_by_scenario[s] for s in scenarios],
        axis=1,
    ).sort_index()

    result.index = result.index.astype(object)

    total_row: Dict[str, Any] = {}
    for scenario in scenarios:
        num_rus_col = f"{scenario}_NumRUs"
        distance_col = f"{scenario}_TotalLinkDistanceKM"

        total_row[num_rus_col] = int(result[num_rus_col].sum())
        total_row[distance_col] = float(result[distance_col].sum())

    result.loc["TOTAL"] = total_row
    result.index.name = "O-DU"

    return result


if __name__ == "__main__":
    # A matriz é carregada apenas uma vez e reutilizada em todos os cenários.
    dm_path = OUT_DIR / f"dm_{Filename}.csv"

    if not dm_path.exists():
        raise FileNotFoundError(
            f"Matriz de distâncias não encontrada: {dm_path}"
        )

    df_dm = pd.read_csv(
        dm_path,
        index_col="NumEstacao",
    )

    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    stats_rows: List[Dict[str, Any]] = []
    odu_tables: Dict[str, pd.DataFrame] = {}

    # Lê todos os arquivos de associação dos cenários/heurísticas.
    for prefixo in ("ilp_", "grd_"):
        padrao = f"{prefixo}{Filename}_*.csv"

        for arquivo_csv in sorted(OUT_DIR.glob(padrao)):
            print(f"Carregando o arquivo {arquivo_csv}")

            clusters = pd.read_csv(arquivo_csv)

            cadeia = arquivo_csv.stem.split(
                f"{prefixo}{Filename}_",
                1,
            )[1]

            # Exemplos: ilp_otimizado e ilp_adversarial.
            scenario = f"{prefixo.rstrip('_')}_{cadeia}"

            clusters = add_link_distance(
                df=clusters,
                df_dm=df_dm,
            )

            stats_rows.append(
                stats(
                    df=clusters,
                    scenario=scenario,
                )
            )

            odu_tables[scenario] = stats_by_odu(
                df=clusters,
                scenario=scenario,
            )

    if not stats_rows:
        raise FileNotFoundError(
            "Nenhum arquivo de associações foi encontrado para os "
            f"padrões ilp_{Filename}_*.csv ou grd_{Filename}_*.csv "
            f"em {OUT_DIR}."
        )

    # Estatísticas gerais: uma linha por cenário.
    stats_df = pd.DataFrame(stats_rows)
    stats_output = OUT_DIR / f"stats_{Filename}.csv"
    stats_df.to_csv(stats_output, index=False)

    print(f"\nEstatísticas gerais gravadas em: {stats_output}")

    # Estatísticas por O-DU: uma linha por O-DU e colunas por cenário.
    stats_by_odus_df = build_stats_by_odus(odu_tables)
    stats_by_odus_output = (
        OUT_DIR / f"stats_by_odus_{Filename}.csv"
    )

    stats_by_odus_df.to_csv(
        stats_by_odus_output,
        index=True,
        float_format="%.6f",
    )

    print(
        "Estatísticas por O-DU gravadas em: "
        f"{stats_by_odus_output}"
    )
