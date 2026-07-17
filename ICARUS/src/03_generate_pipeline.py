import csv
import os
from pathlib import Path
import time
import pandas as pd
import numpy as np
from ICARUS.util.constants import FIBER_DELAY_US_PER_KM, MAX_FIBER_DISTANCE_KM, MAX_LOAD
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()
MAX_CLUSTER_SIZE = int(os.environ["MAX_RUS"])

def generate_csv_to_pipeline(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
    output_filename: Path
) -> None:
    """
    Gera um TXT com uma linha por cluster DU/RU.
    Formato de cada linha:
        BW_DU, 0, BW_RU_1, DELAY_RU_1_US, BW_RU_2, DELAY_RU_2_US, ...

    Onde:
    - BW_DU é a largura de banda da RU que atua como DU.
    - O primeiro delay é sempre 0, pois representa a própria DU.
    - Cada par seguinte representa uma RU atendida pela DU:
        largura_de_banda_RU, delay_fibra_us
    - O delay é calculado a partir da matriz de distância:
        delay_us = distancia_km * fiber_delay_us_per_km

    Parâmetros:
    - df: DataFrame com as RUs e a coluna de auto-relacionamento O-DU.
    - df_dm: matriz de distâncias, indexada por id nas linhas e colunas.
    """

    delay_decimals = 0  # número de casas decimais para os delays em microssegundos

    # Garante ordem estável dos clusters conforme aparecem no df
    du_ids = df.loc[df["id"] == df["O-DU"], "id"].tolist()

    rows = []

    for du_id in du_ids:
        du_row = df.loc[df["id"] == du_id].iloc[0]

        du_id = int(du_row["id"])
        du_bandwidth = int(du_row["bandwidth"])


        # Começa a linha com os dados da própria DU
        row = [
            du_id,
            du_bandwidth,
            0,
        ]

        # RUs associadas a esta DU, exceto a própria DU
        cluster_rus = df.loc[
            (df["O-DU"] == du_id) & (df["id"] != du_id)
        ]

        for _, ru_row in cluster_rus.iterrows():
            ru_id = ru_row["id"]
            ru_bandwidth = ru_row["bandwidth"]

            distance_km = df_dm.loc[ru_id, du_id]
            delay_us = round(distance_km * FIBER_DELAY_US_PER_KM)

            row.extend([
                int(ru_bandwidth),
                int(delay_us),
            ])

        rows.append(row)
    print(f"Gerando arquivo de pipeline em {output_filename}...")
    with output_filename.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

if __name__ == "__main__":
    csv_path = OUT_DIR / f"dm_{Filename}.csv"
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Matriz de distâncias não encontrada: {csv_path}")
    # Carrega a Matriz de Distâncias. Define 'id' como índice para acesso rápido: dists.loc[id_origem, id_destino]
    df_dm = pd.read_csv(csv_path, index_col='NumEstacao')
    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    #abrindo o arquivo resultado da clusterização RU-DU com critério Max Load
    csv_path = OUT_DIR / f"ilp_{Filename}_max_load.csv"
    print(f"Carregando o arquivo {csv_path}")
    df_cluster = pd.read_csv(csv_path)

    #Gerando Arquivo Texto para Pipeline Max Load
    generate_csv_to_pipeline(df_cluster, df_dm, output_filename=OUT_DIR / f"pipeline_{Filename}_max_load.txt")

    #abrindo o arquivo resultado da clusterização RU-DU com critério Total Distance
    csv_path = OUT_DIR / f"ilp_{Filename}_total_distance.csv"
    print(f"Carregando o arquivo {csv_path}")
    df_cluster = pd.read_csv(csv_path)

    #Gerando Arquivo Texto para Pipeline Total Distance
    generate_csv_to_pipeline(df_cluster, df_dm, output_filename=OUT_DIR / f"pipeline_{Filename}_total_distance.txt")

