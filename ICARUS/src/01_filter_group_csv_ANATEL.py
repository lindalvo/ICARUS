import os
from pathlib import Path
import pandas as pd
import chardet
from ICARUS.util.functions import designacao_para_mhz, haversine_distance
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

ROUND_COORD_DECIMALS = 5  # ~1.1m em latitude; longitude ~1.1m*cos(lat)
#Regras de negócio
Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()
ANATEL_DATA_DIR = Path(os.environ["ANATEL_DATA_DIR"]).resolve()


def main() -> int:
    N_total_linhas = 0
    N_NotNR_Servico10 = 0
    N_DesigancoesInvalidas = 0
    N_CoordenadasInvalidas = 0
    csv_path = ANATEL_DATA_DIR / f"{Filename}.csv"
    with open(csv_path, 'rb') as f:
        result = chardet.detect(f.read(100000))
    print(f"Lendo CSV: {csv_path}. Codificação detectada: {result['encoding']}")
    df = pd.read_csv(csv_path, on_bad_lines='skip', low_memory=False, encoding=result['encoding'])
    tamanho_corrente = len(df)
    print(f"Linhas carregadas: {tamanho_corrente}")
    N_total_linhas += tamanho_corrente

    # Filtrando Tecnologia 'NR' NumServico 10 
    df = df[(df['Tecnologia'] == 'NR') & (df['NumServico'] == 10)]
    novo_tamanho = len(df)
    N_NotNR_Servico10 += (tamanho_corrente - novo_tamanho)
    tamanho_corrente = novo_tamanho
    print(f"Linhas após filtro tecnologia NR Serviço Móvel Pessoal: {tamanho_corrente}")

    # Filtrando designações válidas para SCS 30 FR1 (5G NR), conforme tabela de designações ITU. Exemplo: 100MG7W, 50M0G7W, 40M0G7W, etc.
    DesigancoesValidas = {"100MG7W","100MD7W","90M0G7W","80M0G7W","80M0D7W","70M0G7W","60M0G7W","50M0G7W","40M0G7W","30M0G7W","25M0G7W","20M0G7W","20M0D7W","15M0G7W","10M0G7W","5M00G7W"}
    df = df[df["DesignacaoEmissao"].astype(str).str.strip().str.upper().isin(DesigancoesValidas)]
    novo_tamanho = len(df)
    N_DesigancoesInvalidas += (tamanho_corrente - novo_tamanho)
    tamanho_corrente = novo_tamanho
    print(f"Linhas após filtro Designações Válidas: {tamanho_corrente}")

    # Coerção numérica das coordenadas (evita string/NaN quebrando round/groupby)
    df["Latitude"] = pd.to_numeric(df["Latitude"], errors="coerce")
    df["Longitude"] = pd.to_numeric(df["Longitude"], errors="coerce")
    df["NumEstacao"] = pd.to_numeric(df["NumEstacao"], errors="coerce").astype("Int64")
    df = df.dropna(subset=["Latitude", "Longitude"]).copy()
    novo_tamanho = len(df)
    N_CoordenadasInvalidas += (tamanho_corrente - novo_tamanho)
    tamanho_corrente = novo_tamanho
    print(f"Linhas após remover coords inválidas: {tamanho_corrente}")

    # Adicionanado a coluna de PRBs
    df["bandwidth"] = df["DesignacaoEmissao"].apply(lambda x: designacao_para_mhz(str(x)))

    # Arredondamento (agrupa ~1.1 m) de coordenadas para juntar pontos muito próximos
    df["Lat"] = df["Latitude"].round(ROUND_COORD_DECIMALS)
    df["Lon"] = df["Longitude"].round(ROUND_COORD_DECIMALS)
    # coordenada, somando os PRBs de todas as designações daquela coordenada.
    df_grouped = (
        df.groupby(["NumEstacao", "Lat", "Lon"], as_index=False)
            .agg(
                N_Latitude=("Latitude", "nunique"),
                N_Longitude=("Longitude", "nunique"),
                Latitudes=("Latitude", lambda s: ";".join(sorted(set(s.astype(str))))),
                Longitudes=("Longitude", lambda s: ";".join(sorted(set(s.astype(str))))),
                bandwidth=("bandwidth", lambda s: s.mode().iat[0]),
                N_Designacoes=("DesignacaoEmissao", "nunique"),
                Designacoes=("DesignacaoEmissao", lambda s: ";".join(sorted(set(s)))),
                N_Setores=("Azimute", "nunique"),
                Setores=("Azimute", lambda s: ";".join(sorted(set(s.astype(str)))))
            )
    )

    # Força tipo texto
    df_grouped["NumEstacao"] = df_grouped["NumEstacao"].astype("string")

    count_grouped = len(df_grouped)
    #Reordenar colunas
    df_grouped = df_grouped[
        ["NumEstacao", "Lat", "Lon", "bandwidth",
        "N_Latitude", "N_Longitude", "Latitudes", "Longitudes",
        "N_Designacoes", "Designacoes", "N_Setores", "Setores"]
    ]
    print(f"Linhas após agrupamento: {count_grouped}")
    out_path = OUT_DIR / f"grp_{Filename}.csv"
    print(f"\t Salvando CSV Agrupado por RU: {out_path}")
    df_grouped.to_csv(out_path, index=False)
    
    #Criando a matriz de Distâncias Vazia
    out_path = OUT_DIR / f"dm_{Filename}.csv"
    matriz = pd.DataFrame(
        data=0.0,
        index=df_grouped['NumEstacao'],
        columns=df_grouped['NumEstacao']
    )
    for i in range(count_grouped):
        id_i = df_grouped.loc[i, "NumEstacao"]
        lat_i = df_grouped.loc[i, "Lat"]
        lon_i = df_grouped.loc[i, "Lon"]
        for j in range(i, count_grouped):
            id_j = df_grouped.loc[j, "NumEstacao"]
            if i == j:
                matriz.loc[id_i, id_j] = 0.0
            else:
                lat_j = df_grouped.loc[j, "Lat"]
                lon_j = df_grouped.loc[j, "Lon"]
                distance = haversine_distance(lat_i, lon_i, lat_j, lon_j)
                matriz.loc[id_i, id_j] = distance
                matriz.loc[id_j, id_i] = distance

    # Salvando a matriz de distâncias em um arquivo CSV
    print(f"\t Salvando Matriz de distâncias: {out_path}")
    matriz.to_csv(out_path, index=True, index_label="NumEstacao")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
