import csv
import os
from pathlib import Path
import pandas as pd
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()
FIBER_DELAY_US_PER_KM = float(os.environ["FIBER_DELAY_US_PER_KM"])

def generate_csv_to_pipeline(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
    output_filename: Path
) -> None:
    """
    Gera um arquivo TXT com uma linha por cluster O-DU/O-RU.

    Formato de cada linha:
        NUMESTACAO_DU, BW_DU, 0,
        NUMESTACAO_RU_1, BW_RU_1, DELAY_RU_1_US,
        NUMESTACAO_RU_2, BW_RU_2, DELAY_RU_2_US, ...

    O primeiro trio representa a própria O-RU promovida a O-DU:
        - NumEstacao da O-DU;
        - largura de banda da O-DU;
        - delay igual a zero.

    Cada trio seguinte representa uma O-RU atendida pela O-DU:
        - NumEstacao da O-RU;
        - largura de banda da O-RU;
        - delay em microssegundos, calculado a partir da distância até a O-DU.

    O delay é calculado por:
        delay_us = distancia_km * FIBER_DELAY_US_PER_KM

    Parâmetros:
    - df: DataFrame com as colunas NumEstacao, bandwidth e O-DU.
    - df_dm: matriz de distâncias indexada por NumEstacao nas linhas e colunas.
    - output_filename: caminho do arquivo TXT de saída.
    """
    

    df = df.copy()
    df["NumEstacao"] = df["NumEstacao"].astype(int)
    df["O-DU"] = df["O-DU"].astype(int)

    df_dm = df_dm.copy()
    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    # Mantém a ordem em que as DUs aparecem no arquivo de clusterização.
    du_ids = df.loc[df["NumEstacao"] == df["O-DU"], "NumEstacao"].tolist()

    referenced_du_ids = set(df["O-DU"])
    declared_du_ids = set(du_ids)
    rows = []

    for du_id in du_ids:
        du_row = df.loc[df["NumEstacao"] == du_id].iloc[0]
        du_bandwidth = int(du_row["bandwidth"])

        # Primeiro trio: O-RU que foi promovida a O-DU.
        row = [
            int(du_id),
            du_bandwidth,
            0,
        ]

        # Demais O-RUs associadas à O-DU, preservando a ordem do DataFrame.
        cluster_rus = df.loc[
            (df["O-DU"] == du_id) & (df["NumEstacao"] != du_id)
        ]

        for _, ru_row in cluster_rus.iterrows():
            ru_id = int(ru_row["NumEstacao"])
            ru_bandwidth = int(ru_row["bandwidth"])
            distance_km = float(df_dm.loc[ru_id, du_id])
            delay_us = int(round(distance_km * FIBER_DELAY_US_PER_KM))

            row.extend([
                ru_id,
                ru_bandwidth,
                delay_us,
            ])

        rows.append(row)

    print(f"Gerando arquivo de pipeline em {output_filename}...")
    with output_filename.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerows(rows)

    print(
        f"Arquivo gerado com {len(rows)} clusters e "
        f"{len(df)} estações."
    )
    
if __name__ == "__main__":
    csv_path = OUT_DIR / f"dm_{Filename}.csv"
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Matriz de distâncias não encontrada: {csv_path}")
    # Carrega a Matriz de Distâncias. Define 'id' como índice para acesso rápido: dists.loc[id_origem, id_destino]
    df_dm = pd.read_csv(csv_path, index_col='NumEstacao')
    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    padrao = f"ilp_{Filename}_*.csv"
    for arquivo_csv in OUT_DIR.glob(padrao):
        #abrindo o arquivo de  clusterização
        csv_path = arquivo_csv
        print(f"Carregando o arquivo {csv_path}")
        cadeia = arquivo_csv.stem.split(f"ilp_{Filename}_", 1)[1]
        df_cluster = pd.read_csv(csv_path)
        generate_csv_to_pipeline(df_cluster, df_dm, output_filename=OUT_DIR / f"pipeline_{Filename}_{cadeia}.txt")

