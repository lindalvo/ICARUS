import os
from pathlib import Path
import pandas as pd
import numpy as np
from typing import Any, Dict, List
from dotenv import find_dotenv, load_dotenv


load_dotenv(find_dotenv())

Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()


def stats(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
    scenario: str
) -> Dict[str, Any]:
    """
    Calcula e retorna as estatísticas básicas de um cenário de clusterização.
    """

    # --- Distâncias RU -> DU ---
    dist_ru_du = []

    for ru_id, du_id in zip(
        df["NumEstacao"].to_list(),
        df["O-DU"].to_list()
    ):
        d = float(df_dm.at[ru_id, du_id])
        dist_ru_du.append(d)

    dist_ru_du = np.array(dist_ru_du, dtype=float)

    # Soma das distâncias RU -> DU.
    # As O-DUs associadas a si mesmas contribuem com distância zero.
    total_enlace_km = float(dist_ru_du.sum())

    # Considera apenas enlaces entre pontos distintos.
    dist_links = dist_ru_du[
        df["NumEstacao"].values != df["O-DU"].values
    ]

    media_dist_ru_du = float(dist_links.mean())
    dp_dist_ru_du = float(dist_links.std(ddof=1))

    # --- Quantidade de DUs / clusters ---
    qtde_dus = int(df["O-DU"].nunique())

    # --- RUs por DU ---
    # Inclui a própria O-DU no cluster.
    rus_por_du = (
        df.groupby("O-DU")["NumEstacao"]
        .count()
        .astype(int)
    )

    media_qtde_ru_du = float(rus_por_du.mean())
    dp_qtde_ru_du = float(rus_por_du.std(ddof=1))

    # --- Largura de banda por DU ---
    bandwidth_por_du = (
        df.groupby("O-DU")["bandwidth"]
        .sum()
        .astype(float)
    )

    media_bandwidth_du = float(bandwidth_por_du.mean())
    dp_bandwidth_du = float(bandwidth_por_du.std(ddof=1))

    # --- Quantidade total de pontos ---
    qtde_pontos = int(len(df))

    # --- Impressão das estatísticas ---
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


if __name__ == "__main__":
    # --- Matriz de distâncias ---
    dm_path = OUT_DIR / f"dm_{Filename}.csv"

    if not dm_path.exists():
        raise FileNotFoundError(
            f"Matriz de distâncias não encontrada: {dm_path}"
        )

    df_dm = pd.read_csv(
        dm_path,
        index_col="NumEstacao"
    )

    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    # Cada elemento da lista será uma linha do CSV final.
    stats_rows: List[Dict[str, Any]] = []

    # --- Arquivos de clusterização ---
    for prefixo in ("ilp_", "grd_"):
        padrao = f"{prefixo}{Filename}_*.csv"

        for arquivo_csv in sorted(OUT_DIR.glob(padrao)):
            print(f"Carregando o arquivo {arquivo_csv}")

            clusters = pd.read_csv(arquivo_csv)

            cadeia = arquivo_csv.stem.split(
                f"{prefixo}{Filename}_",
                1
            )[1]

            # Exemplo: ilp_opex_capex ou grd_cpu_power
            scenario = f"{prefixo.rstrip('_')}_{cadeia}"

            scenario_stats = stats(
                df=clusters,
                df_dm=df_dm,
                scenario=scenario
            )

            stats_rows.append(scenario_stats)

    # --- CSV único com todos os cenários ---
    stats_df = pd.DataFrame(stats_rows)

    output_path = OUT_DIR / f"stats_{Filename}.csv"
    stats_df.to_csv(output_path, index=False)

    print(f"\nEstatísticas gravadas em: {output_path}")