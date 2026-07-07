import csv
import os
from pathlib import Path
import time
import pandas as pd
import numpy as np
import folium
from typing import Any, Dict, List
from pyomo.environ import (
    ConcreteModel, Set, Param, Var, Binary, NonNegativeReals,
    Objective, Constraint, minimize, value
)
from pyomo.opt import SolverFactory, TerminationCondition
from ICARUS.util.constants import FIBER_DELAY_US_PER_KM, OUT_DIR, Filename, MAX_FIBER_DISTANCE_KM, MAX_LOAD, MAX_CLUSTER_SIZE

def cluster_ilp(df: pd.DataFrame, df_dm: pd.DataFrame, objective_mode: str):
    """
    Clusteriza RUs em DUs usando Programação Linear Inteira via Pyomo.

    Objetivos:
    Primário: Minimizar o número de DUs;
    Secundário:   "total_distance" -> minimiza soma total das distâncias RU-DU.
                  "max_link"       -> minimiza o maior enlace individual RU-DU.

    Restrições:
    1) Cada RU deve ser atendida por exatamente uma DU;
    2) Distância RU-DU <= MAX_FIBER_DISTANCE_KM;
    3) Soma das larguras de banda por DU <= MAX_LOAD;
    4) Cada cluster pode ter no máximo MAX_CLUSTER_SIZE elementos, incluindo a própria DU.
    """

    # Preparar Variáveis
    ids = df["id"].astype(int).tolist()
    id_set = set(ids)
    loads = df.set_index("id")["bandwidth"].astype(float).to_dict()

    # Pares viáveis (i,j): d(ij)) <= MAX_FIBER_DISTANCE_KM
    valid_pairs = []
    distances = {} # Dicionário rápido para acesso (i, j) -> dist
    neighbors_by_i = {i: [] for i in ids}
    incoming_by_j = {j: [] for j in ids}

    for i in ids:
        # Pega a linha da matriz de distancias
        dists_i = df_dm.loc[i]
        # Filtra dist <= MAX_FIBER_DISTANCE_KM
        valid_neighbors = [
            int(j)
            for j in dists_i[dists_i <= MAX_FIBER_DISTANCE_KM].index
            if int(j) in id_set
        ]
        if not valid_neighbors:
            raise ValueError(
                f"A RU {i} não possui nenhuma DU viável dentro de "
                f"{MAX_FIBER_DISTANCE_KM} km."
            )
        for j in valid_neighbors:
            valid_pairs.append((i, j))
            distances[(i, j)] = float(dists_i[j])
            neighbors_by_i[i].append(j)
            incoming_by_j[j].append(i)
        
    print(f"Iniciando modelagem ILP para {len(ids)} estações...")
    n_valid_pairs = len(valid_pairs)
    print(f"Número de pares válidos (i,j) para conexão: {n_valid_pairs}")

    # --- 2. Criação do Modelo  ---
    m = ConcreteModel("RAN_Clustering_ILP")

    m.I = Set(initialize=ids, ordered=True)
    m.A = Set(dimen=2, initialize=valid_pairs, ordered=False)
    
    #Parametros
    m.ru_load = Param(
        m.I,
        initialize=lambda mm, i: float(loads[i]),
        within=NonNegativeReals,
        mutable=False
    )

    m.dist = Param(
        m.A,
        initialize=lambda mm, i, j: float(distances[(i, j)]),
        within=NonNegativeReals,
        mutable=False
    )

    # Variáveis de Decisão
    # y[j] = 1 se j é DU
    m.y = Var(m.I, domain=Binary)        # 1 se j é DU
    # x[i,j] = 1 se a RU i é atendida pela DU j
    m.x = Var(m.A, domain=Binary)        

    # Restrições ---
    # 1) Cada RU i deve ser atendida por exatamente uma DU viável.
    def assign_rule(mm, i):
        return sum(mm.x[i, j] for j in neighbors_by_i[i]) == 1

    m.Assign = Constraint(m.I, rule=assign_rule)

    # 2) Capacidade de banda por DU.
    # Se y[j] = 0, ninguém pode ser atribuído a j.
    # Se y[j] = 1, a soma das bandas atribuídas a j deve ser <= MAX_LOAD.
    def cap_rule(mm, j):
        return (
            sum(mm.ru_load[i] * mm.x[i, j] for i in incoming_by_j[j])
            <= float(MAX_LOAD) * mm.y[j]
        )

    m.Capacity = Constraint(m.I, rule=cap_rule)

    # 3) Ligação lógica: se i conecta em j, então j deve ser DU.
    def link_rule(mm, i, j):
        return mm.x[i, j] <= mm.y[j]

    m.Link = Constraint(m.A, rule=link_rule)

    # 4) Se j é DU, a própria RU j deve pertencer ao cluster j.
    # Isso garante que a própria DU seja contada na capacidade e na cardinalidade.
    def self_assignment_rule(mm, j):
        return mm.x[j, j] == mm.y[j]

    m.SelfAssignment = Constraint(m.I, rule=self_assignment_rule)

    # 5) Cada cluster pode ter no máximo 5 elementos, incluindo a própria DU.
    def max_cluster_size_rule(mm, j):
        return (
            sum(mm.x[i, j] for i in incoming_by_j[j])
            <= MAX_CLUSTER_SIZE * mm.y[j]
        )

    m.MaxClusterSize = Constraint(m.I, rule=max_cluster_size_rule)

    # Configurações do SOLVER
    TIME_LIMIT_SEC = int(n_valid_pairs * 16)
    SOLVER = 'cbc'  # 'cbc', 'glpk', 'gurobi', 'cplex'
    THREADS = os.cpu_count() or 4

    def configure_solver():
        opt = SolverFactory(SOLVER)
        opt.options.clear()
        match SOLVER:
            case "cbc":
                opt.options.clear()
                opt.options["seconds"] = int(TIME_LIMIT_SEC)
                opt.options["timeMode"] = "elapsed"
                opt.options["threads"] = int(THREADS)
            case "glpk":
                opt.options.clear()
                opt.options["tmlim"] = int(TIME_LIMIT_SEC)
            case "gurobi" | "gurobi_direct" | "gurobi_persistent":
                opt.options.clear()
                #opt.options["TimeLimit"] = float(TIME_LIMIT_SEC)
                opt.options["Threads"] = int(THREADS)
            case "cplex" | "cplex_direct" | "cplex_persistent":
                opt.options.clear()
                opt.options["timelimit"] = float(TIME_LIMIT_SEC)
                opt.options["threads"] = int(THREADS)
        return opt
    
    # --- Resolução Etapa 1 - minimização do número de DUs ---
    #adicionando o objetico ao modelo
    m.Stage1OBJ = Objective(
        expr=sum(m.y[j] for j in m.I),
        sense=minimize,
    )

    print(f"Minimizando o número de DUs com solver {SOLVER} (threads={THREADS})...")
    print(f"Tempo limite {TIME_LIMIT_SEC} segundos)...")
    
    opt = configure_solver()
    inicio = time.perf_counter()
    results = opt.solve(m, tee=True)
    fim = time.perf_counter()
    print(f"Tempo de resolução primeira etapa: {fim - inicio:.2f} segundos.")
    term = results.solver.termination_condition
    print(f"TerminationCondition: {term}.")
    if term != TerminationCondition.optimal:
        raise RuntimeError(f"Minimização do número de DUs não provou otimalidade. TerminationCondition: {term}. Para garantir número mínimo de DUs, a primeira etapa precisa ser ótima.")
    best_du_count = int(round(sum(value(m.y[j]) for j in m.I)))
    print(f"Número mínimo de DUs encontrado: {best_du_count}")

    # Resolução Etapa 2 otimizar objetivo secundário passado como parâmetro
    #adicionado a restrição de número mínimo de DUs ao modelo
    m.FixDUCount = Constraint(
        expr=sum(m.y[j] for j in m.I) == best_du_count
    )
    #desativar o objetivo primário
    m.Stage1OBJ.deactivate()

    msg = ""
    #adicionar o objetivo secundário
    if objective_mode == "total_distance":
        msg = "Minimizando a soma total das distâncias RU-DU."
        m.Stage2OBJ = Objective(
            expr=sum(
                m.dist[i, j] * m.x[i, j]
                for (i, j) in m.A
            ),
            sense=minimize
        )

    elif objective_mode == "max_link":
        msg = "Minimizando o maior enlace individual RU-DU."
        m.MaxLinkDistance = Var(domain=NonNegativeReals)

        def max_link_rule(mm, i, j):
            return mm.MaxLinkDistance >= mm.dist[i, j] * mm.x[i, j]

        m.MaxLinkConstraint = Constraint(m.A, rule=max_link_rule)

        m.Stage2OBJ = Objective(
            expr=m.MaxLinkDistance,
            sense=minimize,
        )
    
    print(f"{msg} com solver {SOLVER} (threads={THREADS})...")
    print(f"Tempo limite {TIME_LIMIT_SEC} segundos)...")

    opt = configure_solver()
    inicio = time.perf_counter()
    results = opt.solve(m, tee=True)
    fim = time.perf_counter()
    print(f"Tempo de resolução segunda etapa: {fim - inicio:.2f} segundos.")
    term = results.solver.termination_condition
    print(f"TerminationCondition: {term}.")

    # Aceita ótimo, ou interrupção por limite de tempo com incumbente
    ok_terms = {
        TerminationCondition.optimal,
        TerminationCondition.maxTimeLimit,
        TerminationCondition.feasible
    }

    if term not in ok_terms:
        raise TimeoutError(f"Solver terminou em estado não aceito: {term}; descartando.")

    assignment = {}

    for i in ids:
        chosen = None
        for j in neighbors_by_i[i]:
            xv = value(m.x[i, j])
            if xv is not None and xv > 0.5:
                chosen = j
                break
        if chosen is None:
            raise RuntimeError(f"Não foi possível extrair atribuição viável para a RU {i}.")
        assignment[i] = int(chosen)

    df["O-DU"] = df["id"].astype(int).map(assignment)
    return df


def stats(df, df_dm, output_filename: Path):
    #calcula, imprime e salva estatísticas básicas
    rows: List[Dict[str, Any]] = []

    # --- Distâncias RU->DU ---
    dist_ru_du = []
    for ru_id, du_id in zip(df["id"].to_list(), df["O-DU"].to_list()):
        d = float(df_dm.at[ru_id, du_id])
        dist_ru_du.append(d)

    dist_ru_du = np.array(dist_ru_du, dtype=float)

    # Total enlace: soma das distâncias RU->DU (DUs contribuem com 0)
    total_enlace_km = float(dist_ru_du.sum())    

    # Média/DP das distâncias RU->DU
    # Se você NÃO quiser que DUs (0 km) puxem a média para baixo, exclua ru_id==du_id:
    dist_links = dist_ru_du[(df["id"].values != df["O-DU"].values)]  # só RU realmente “ligadas” a uma DU distinta
    media_dist_ru_du = float(dist_links.mean())
    dp_dist_ru_du = float(dist_links.std(ddof=1))

    # --- Quantidade de DUs / clusters ---
    qtde_dus = int(df["O-DU"].nunique())

    # --- RUs por DU ---
    rus_por_du = df.groupby("O-DU")["id"].count().astype(int)   # inclui a própria DU
    media_qtde_ru_du = float(rus_por_du.mean()) 
    dp_qtde_ru_du = float(rus_por_du.std(ddof=1)) 

    # --- Largura de Banda por DU (soma das cargas no cluster, incluindo a DU) ---
    bandwidth_por_du = df.groupby("O-DU")["bandwidth"].sum().astype(float)
    media_bandwidth_du = float(bandwidth_por_du.mean())
    dp_bandwidth_du = float(bandwidth_por_du.std(ddof=1))

    # --- Quantidade total de pontos ---
    qtde_pontos = int(len(df))

    #imprmindo as estatísticas
    print("\n--- Estatísticas da Clusterização ---")
    print(f"Quantidade total de pontos (RUs + DUs): {qtde_pontos}")
    print(f"Quantidade de DUs (clusters): {qtde_dus}")
    print(f"Média de RUs por DU (cluster): {media_qtde_ru_du:.2f} (DP: {dp_qtde_ru_du:.2f})")
    print(f"Média de Largura de Banda por DU (carga total do cluster): {media_bandwidth_du:.2f} (DP: {dp_bandwidth_du:.2f})")
    print(f"Distância total de enlace (soma RU->DU): {total_enlace_km:.2f} km")
    print(f"Média de distância RU->DU (excluindo DUs ligadas a si mesmas): {media_dist_ru_du:.2f} km (DP: {dp_dist_ru_du:.2f} km)")

    #salvando as estatísticas em um arquivo CSV
    rows.append({
        "TotalPoints": qtde_pontos,
        "NumDUs": qtde_dus,
        "MediaRUsPerDU": media_qtde_ru_du,
        "DP_RUsPerDU": dp_qtde_ru_du,
        "MediaBandwidthPerDU": media_bandwidth_du,
        "DP_BandwidthPerDU": dp_bandwidth_du,
        "TotalLinkDistanceKM": total_enlace_km,
        "MediaLinkDistance": media_dist_ru_du,
        "DP_LinkDistance": dp_dist_ru_du,
    })

    stats_df = pd.DataFrame(rows)
    stats_df.to_csv(output_filename, index=False)
    return 0

def check_rules(df, df_dm):
    #checa se as regras de clusterização foram respeitadas
    ok = True
    # Regra 1: Distância RU -> DU <= MAX_FIBER_DISTANCE_KM
    dist_violations: List[Dict[str, Any]] = []
    ru_ids = df["id"].to_numpy()
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
            dist_violations.append({"id": ru_ids[k], "o_du": du_ids[k], "dist_km": float(dist[k])})
            print(f"[VIOL-1] : distâncias acima do limite {MAX_FIBER_DISTANCE_KM} km "
                  f"({len(dist_violations)} violações)")
            for v in dist_violations[:10]:  # mostra só as 10 mais graves
                print(f"  - RU {v['id']} -> DU {v['o_du']} : {v['dist_km']:.6f} km")

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
    id_set = set(df["id"].unique())

    du_self_violations: List[Dict[str, Any]] = []
    for du in sorted(du_set):
        if du not in id_set:
            du_self_violations.append({"du": du, "reason": "DU não existe na coluna id", "found_o_du": None})
            continue

        found = df.loc[df["id"] == du, "O-DU"].iloc[0]
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
            members = df.loc[df["O-DU"] == du, "id"].tolist()

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

def generate_map(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
    output_filename: Path
):
    """
    Gera um mapa HTML da clusterização ILP RU -> O-DU.
    """
    tiles: str = "OpenStreetMap"
    df = df.copy()
    df["id"] = df["id"].astype(int)
    df["O-DU"] = df["O-DU"].astype(int) 
    df_by_id = df.set_index("id", drop=False)
    du_ids = set(df.loc[df["id"] == df["O-DU"], "id"])

    # Centro inicial do mapa
    center_lat = df["Lat"].mean()
    center_lon = df["Lon"].mean()

    mapa = folium.Map(
        location=[center_lat, center_lon],
        zoom_start=12,
        tiles=tiles,
        control_scale=True,
    )

    fg_du = folium.FeatureGroup(name="DUs", show=True)
    fg_ru = folium.FeatureGroup(name="RUs", show=True)
    fg_links = folium.FeatureGroup(name="Conexões RU-DU", show=True)
    fg_labels = folium.FeatureGroup(name="Labels", show=True)

    # ------------------------------------------------------------------
    # 1. Linhas RU -> DU com label de distância
    # ------------------------------------------------------------------
    for _, row in df.iterrows():
        ru_id = row["id"]
        du_id = row["O-DU"]

        # Não desenha linha da DU para ela mesma
        if ru_id == du_id:
            continue

        ru_lat = row["Lat"]
        ru_lon = row["Lon"]

        du_row = df_by_id.loc[du_id]
        du_lat = du_row["Lat"]
        du_lon = du_row["Lon"]

        distancia_km = float(df_dm.loc[ru_id, du_id])

        # Linha RU -> DU
        linha = folium.PolyLine(
            locations=[
                [ru_lat, ru_lon],
                [du_lat, du_lon],
            ],
            color="blue",
            weight=1,
            opacity=0.65,
            tooltip=(
                f"RU {ru_id} → DU {du_id}<br>"
                f"Distância: {distancia_km:.3f} km"
            ),
        )
        linha.add_to(fg_links)

        # Label fixo da distância no ponto médio da linha
        mid_lat = (ru_lat + du_lat) / 2
        mid_lon = (ru_lon + du_lon) / 2

        folium.Marker(
            location=[mid_lat, mid_lon],
            icon=folium.DivIcon(
                html=f"""
                <div style="
                    font-size: 9px;
                    color: #003366;
                    background-color: rgba(255, 255, 255, 0.85);
                    border: 1px solid #003366;
                    border-radius: 4px;
                    padding: 2px 4px;
                    white-space: nowrap;">
                    {distancia_km:.2f} km
                </div>
                """
            ),
        ).add_to(fg_labels)

    # ------------------------------------------------------------------
    # 2. Marcadores de DUs e RUs
    # ------------------------------------------------------------------
    for _, row in df.iterrows():
        station_id = row["id"]
        lat = row["Lat"]
        lon = row["Lon"]
        bandwidth = row["bandwidth"]
        assigned_du = row["O-DU"]

        is_du = station_id == assigned_du

        popup_html = f"""
        <b>Estação:</b> {station_id}<br>
        <b>Tipo:</b> {"DU" if is_du else "RU"}<br>
        <b>Bandwidth:</b> {bandwidth} MHz<br>
        <b>O-DU associada:</b> {assigned_du}
        """

        if is_du:
            marker = folium.Marker(
                location=[lat, lon],
                tooltip=f"DU {station_id} | BW: {bandwidth} MHz",
                popup=folium.Popup(popup_html, max_width=300),
                icon=folium.Icon(
                    color="red",
                    icon="server",
                    prefix="fa",
                ),
            )
            marker.add_to(fg_du)

            label_text = f"DU {station_id}<br>{bandwidth} MHz"
            label_color = "#8B0000"
            border_color = "#8B0000"

        else:
            marker = folium.Marker(
                location=[lat, lon],
                tooltip=f"RU {station_id} | BW: {bandwidth} MHz | DU: {assigned_du}",
                popup=folium.Popup(popup_html, max_width=300),
                icon=folium.Icon(
                    color="green",
                    icon="wifi",
                    prefix="fa",
                ),
            )
            marker.add_to(fg_ru)

            label_text = f"RU {station_id}<br>{bandwidth} MHz"
            label_color = "#006400"
            border_color = "#006400"

        # Label fixo de bandwidth ao lado do marcador
        folium.Marker(
            location=[lat, lon],
            icon=folium.DivIcon(
                icon_anchor=(-8, 18),
                html=f"""
                <div style="
                    font-size: 9px;
                    color: {label_color};
                    background-color: rgba(255, 255, 255, 0.85);
                    border: 1px solid {border_color};
                    border-radius: 4px;
                    padding: 2px 4px;
                    white-space: nowrap;">
                    {label_text}
                </div>
                """
            ),
        ).add_to(fg_labels)

    fg_links.add_to(mapa)
    fg_du.add_to(mapa)
    fg_ru.add_to(mapa)
    fg_labels.add_to(mapa)

    folium.LayerControl(collapsed=False).add_to(mapa)

    # Ajusta o zoom para cobrir todas as estações
    bounds = df[["Lat", "Lon"]].values.tolist()
    mapa.fit_bounds(bounds)
    print(f"Salvando mapa interativo em {output_filename}...")
    mapa.save(output_filename)

    return True

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
    #abrindo o arquivo de dados de entrada (RUs + DUs) para clusterização
    csv_path = OUT_DIR / f"grp_{Filename}.csv"
    print(f"Carregando o arquivo {csv_path}")
    df = pd.read_csv(csv_path)

    #Removendo colunas desnecessárias
    df.drop(columns=['N_Latitude','N_Longitude','Latitudes','Longitudes','N_Designacoes','Designacoes','N_Setores','Setores'], inplace=True)
    df["id"] = df["id"].astype(int)
    csv_path = OUT_DIR / f"dm_{Filename}.csv"
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Matriz de distâncias não encontrada: {csv_path}")
    # Carrega a Matriz de Distâncias. Define 'id' como índice para acesso rápido: dists.loc[id_origem, id_destino]
    df_dm = pd.read_csv(csv_path, index_col='id')
    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    df_cluster = cluster_ilp(df, df_dm, objective_mode="total_distance")
    output_filename = OUT_DIR / f"ilp_{Filename}_total_distance.csv"
    print(f"Gravando o resultado da clusterização em {output_filename}")
    df_cluster.to_csv(output_filename, index=False)

    #ou. se já tiver sido gerado em execução anterior, pode ser carregado diretamente:
    #print(f"Carregando clusterização de {output_filename}")
    #df_cluster = pd.read_csv(output_filename)

    #gravando as estatísticas em um arquivo CSV
    stats(df_cluster, df_dm, output_filename=OUT_DIR / f"stats_{Filename}_total_distance.csv")

    #checando regras do cluster foram respeitadas
    check_rules(df_cluster, df_dm)
    
    #Gerando Mapa
    generate_map(df_cluster, df_dm, output_filename=OUT_DIR / f"map_{Filename}_total_distance.html")

    #Gerando Arquivo Texto para Pipeline
    generate_csv_to_pipeline(df_cluster, df_dm, output_filename=OUT_DIR / f"pipeline_{Filename}_total_distance.txt")

    df_cluster = cluster_ilp(df, df_dm, objective_mode="max_link")
    output_filename = OUT_DIR / f"ilp_{Filename}_max_link.csv"
    print(f"Gravando o resultado da clusterização em {output_filename}")
    df_cluster.to_csv(output_filename, index=False)

    #ou. se já tiver sido gerado em execução anterior, pode ser carregado diretamente:
    #print(f"Carregando clusterização de {output_filename}")
    #df_cluster = pd.read_csv(output_filename)
    
    #gravando as estatísticas em um arquivo CSV
    stats(df_cluster, df_dm, output_filename=OUT_DIR / f"stats_{Filename}_max_link.csv")

    #checando regras do cluster foram respeitadas
    check_rules(df_cluster, df_dm)
    
    #Gerando Mapa
    generate_map(df_cluster, df_dm, output_filename=OUT_DIR / f"map_{Filename}_max_link.html")

    #Gerando Arquivo Texto para Pipeline
    generate_csv_to_pipeline(df_cluster, df_dm, output_filename=OUT_DIR / f"pipeline_{Filename}_max_link.txt")

