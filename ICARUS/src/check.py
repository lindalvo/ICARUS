import os
from pathlib import Path
import pandas as pd
import numpy as np
from typing import Any, Dict, List
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()
MAX_CLUSTER_SIZE = int(os.environ["MAX_RUS"])
MAX_FIBER_DISTANCE_KM = float(os.environ["MAX_FIBER_DISTANCE_KM"])
MAX_LOAD = int(os.environ["MAX_LOAD"])

def check_rules(df, df_dm):
    #checa se as regras de clusterização foram respeitadas
    ok = True
    # Regra 1: Distância RU -> DU <= MAX_FIBER_DISTANCE_KM
    dist_violations: List[Dict[str, Any]] = []
    ru_ids = df["NumEstacao"].to_numpy()
    du_ids = df["O-DU"].to_numpy()

    dm_index = pd.Index(df_dm.index)
    dm_cols = pd.Index(df_dm.columns)

    i_pos = dm_index.get_indexer(ru_ids)
    j_pos = dm_cols.get_indexer(du_ids)

    df_dm_values = df_dm.to_numpy(dtype=float)

    dist = df_dm_values[i_pos, j_pos]
    viol_mask = dist > (MAX_FIBER_DISTANCE_KM) 
    if np.any(viol_mask):
        ok = False
        viol_idx = np.where(viol_mask)[0]
        # ordena pelas maiores distâncias e limita saída
        viol_idx = viol_idx[np.argsort(dist[viol_idx])[::-1]]
        for k in viol_idx:
            dist_violations.append({"NumEstacao": ru_ids[k], "o_du": du_ids[k], "dist_km": float(dist[k])})
            print(f"[VIOL-1] : distâncias acima do limite {MAX_FIBER_DISTANCE_KM} km "
                  f"({len(dist_violations)} violações)")
            for v in dist_violations[:10]:  # mostra só as 10 mais graves
                print(f"  - RU {v['NumEstacao']} -> DU {v['o_du']} : {v['dist_km']:.6f} km")

    # Regra 2: carga por DU
    loads = df.groupby("O-DU", as_index=True)["bandwidth"].sum()
    over = loads[loads > (MAX_LOAD)]
    if not over.empty:
        ok = False
        over = over.sort_values(ascending=False)
        print(f"[VIOL-2] : DUs com carga acima do limite {MAX_LOAD} "
              f"({len(over)} violações)")
        for du, val in over.head(10).items():
            print(f"  - DU {du}: load={float(val):.6f} (excesso={float(val - MAX_LOAD):.6f})")

    # Regra 3: se alguém aponta para j, então j aponta para j
    du_set = set(df["O-DU"].unique())
    id_set = set(df["NumEstacao"].unique())

    du_self_violations: List[Dict[str, Any]] = []
    for du in sorted(du_set):
        if du not in id_set:
            du_self_violations.append({"du": du, "reason": "DU não existe na coluna id", "found_o_du": None})
            continue

        found = df.loc[df["NumEstacao"] == du, "O-DU"].iloc[0]
        if found != du:
            du_self_violations.append({"du": du, "reason": "DU não aponta para si (cascata)", "found_o_du": found})

    if du_self_violations:
        ok = False
        print(f"[VIOL-3] : violação de auto-referência DU (cascata/ausente) "
              f"({len(du_self_violations)} violações).")
        for v in du_self_violations[:10]:
            print(f"  - DU {v['du']}: {v['reason']} (O-DU encontrado={v['found_o_du']})")

    # Regra 4: tamanho máximo do cluster
    cluster_sizes = df.groupby("O-DU", as_index=True).size()
    over_size = cluster_sizes[cluster_sizes > MAX_CLUSTER_SIZE]

    size_violations: List[Dict[str, Any]] = []

    if not over_size.empty:
        ok = False
        over_size = over_size.sort_values(ascending=False)

        for du, size in over_size.items():
            members = df.loc[df["O-DU"] == du, "NumEstacao"].tolist()

            size_violations.append({
                "du": du,
                "cluster_size": int(size),
                "excess": int(size - MAX_CLUSTER_SIZE),
                "members": members
            })

        print(f"[VIOL-4] : DUs com mais de {MAX_CLUSTER_SIZE} RUs associadas "
              f"contando a própria DU ({len(size_violations)} violações)")
        for v in size_violations[:10]:
            print(f"  - DU {v['du']}: cluster_size={v['cluster_size']} "
                  f"(excesso={v['excess']})")
            print(f"    membros={v['members'][:10]}")

    if ok:
        print(f"[OK]: sem violações. "
              f"(MAX_DIST={MAX_FIBER_DISTANCE_KM} km, "
              f"MAX_LOAD={MAX_LOAD}, "
              f"MAX_CLUSTER_SIZE={MAX_CLUSTER_SIZE})")
    else:
        print(f"[RESUMO]: "
              f"{len(dist_violations)} viol. distâncias, "
              f"{len(over)} viol. carga, "
              f"{len(du_self_violations)} viol. auto-referência, "
              f"{len(size_violations)} viol. tamanho cluster.")
    return ok


if __name__ == "__main__":
    csv_path = OUT_DIR / f"dm_{Filename}.csv"
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Matriz de distâncias não encontrada: {csv_path}")
    # Carrega a Matriz de Distâncias. Define 'NumEstacao' como índice para acesso rápido: dists.loc[NumEstacao_origem, NumEstacao_destino]
    df_dm = pd.read_csv(csv_path, index_col='NumEstacao')
    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)    
    #abrindo os arquivos de clusterização
    for prefixo in ("ilp_", "grd_"):
        padrao = f"{prefixo}{Filename}_*.csv"
        for arquivo_csv in OUT_DIR.glob(padrao):
            #abrindo o arquivo de  clusterização
            csv_path = arquivo_csv
            print(f"Carregando o arquivo {csv_path}")
            df = pd.read_csv(csv_path)
            #checando regras do cluster foram respeitadas
            check_rules(df, df_dm)

