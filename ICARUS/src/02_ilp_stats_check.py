import os
from pathlib import Path
import time
import pandas as pd
import numpy as np
from typing import Any, Dict, List
from pyomo.environ import (
    ConcreteModel, Set, Param, Var, Binary, NonNegativeReals,
    Objective, Constraint, Expression, minimize, value
)
from pyomo.opt import SolverFactory, TerminationCondition
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())
Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()
MAX_CLUSTER_SIZE = int(os.environ["MAX_RUS"])
MAX_FIBER_DISTANCE_KM = float(os.environ["MAX_FIBER_DISTANCE_KM"])
MAX_LOAD = int(os.environ["MAX_LOAD"])
TIME_LIMIT_SOLVER = int(os.environ.get("TIME_LIMIT_SOLVER", 3200))
MAX_SOLVER_THREADS = int(os.environ.get("MAX_SOLVER_THREADS", 32))
def cluster_ilp_primario(df: pd.DataFrame, df_dm: pd.DataFrame):
    """
    Clusteriza RUs em DUs usando Programação Linear Inteira via Pyomo.

    Objetivo Primário: Minimizar o número de DUs;

    Restrições:
    1) Cada RU deve ser atendida por exatamente uma DU;
    2) Distância RU-DU <= MAX_FIBER_DISTANCE_KM;
    3) Soma das larguras de banda por DU <= MAX_LOAD;
    4) Cada cluster pode ter no máximo MAX_CLUSTER_SIZE elementos, incluindo a própria DU.
    """

    # Preparar Variáveis
    NumEstacoes = df["NumEstacao"].astype(int).tolist()
    id_set = set(NumEstacoes)
    loads = df.set_index("NumEstacao")["bandwidth"].astype(float).to_dict()

    # Pares viáveis (i,j): d(ij)) <= MAX_FIBER_DISTANCE_KM
    valid_pairs = []
    distances = {} # Dicionário rápido para acesso (i, j) -> dist
    neighbors_by_i = {i: [] for i in NumEstacoes}
    incoming_by_j = {j: [] for j in NumEstacoes}

    for i in NumEstacoes:
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
        
    print(f"Iniciando modelagem ILP para {len(NumEstacoes)} estações...")
    print(f"Critério de distância máxima para pares RU-DU: {MAX_FIBER_DISTANCE_KM} km")
    print(f"Critério de carga máxima por DU: {MAX_LOAD} MHz")
    print(f"Critério de tamanho máximo de cluster: {MAX_CLUSTER_SIZE} RUs (incluindo a própria DU)")
    n_valid_pairs = len(valid_pairs)
    print(f"Número de pares válidos (i,j) para conexão: {n_valid_pairs}")

    # --- 2. Criação do Modelo  ---
    m = ConcreteModel("ICARUS_Primario")

    m.I = Set(initialize=NumEstacoes, ordered=True)
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

    # 5) Cada cluster pode ter no máximo MAX_CLUSTER_SIZE elementos, incluindo a própria DU.
    def max_cluster_size_rule(mm, j):
        return (
            sum(mm.x[i, j] for i in incoming_by_j[j])
            <= MAX_CLUSTER_SIZE * mm.y[j]
        )

    m.MaxClusterSize = Constraint(m.I, rule=max_cluster_size_rule)

    # Configurações do SOLVER
    SOLVER = 'cbc'  # 'cbc', 'glpk', 'gurobi', 'cplex'
    MAX_SOLVER_THREADS = 32
    THREADS = min(os.cpu_count() or 4, MAX_SOLVER_THREADS)

    opt = SolverFactory(SOLVER)
    opt.options.clear()
    opt.options["ratio"] = 0.001
    opt.options["threads"] = int(THREADS)
    match SOLVER:
        case "cbc":
            opt.options["seconds"] = int(TIME_LIMIT_SOLVER)
            opt.options["timeMode"] = "elapsed"
        case "glpk":
            opt.options["tmlim"] = int(TIME_LIMIT_SOLVER)
        case "gurobi" | "gurobi_direct" | "gurobi_persistent":
            opt.options["TimeLimit"] = float(TIME_LIMIT_SOLVER)
        case "cplex" | "cplex_direct" | "cplex_persistent":
            opt.options["timelimit"] = float(TIME_LIMIT_SOLVER)
            
    
    # --- Objetivo Primário - minimização do número de DUs ---
    #adicionando o objetico ao modelo
    m.Stage1OBJ = Objective(
        expr=sum(m.y[j] for j in m.I),
        sense=minimize,
    )

    print(f"Minimizando o número de DUs com solver {SOLVER} (threads={THREADS})...")
    print(f"Tempo limite {TIME_LIMIT_SOLVER} segundos)...")
    
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
    return best_du_count

def cluster_ilp_secundario(df: pd.DataFrame, df_dm: pd.DataFrame, best_du_count: int, objective_mode: str):
    """
    Clusteriza RUs em DUs usando Programação Linear Inteira via Pyomo.

    Objetivos Secundário:   "opex_capex" -> minimiza soma total das distâncias RU-DU.
                            "cpu_power"       -> minimiza a maior carga agregada atribuída a uma DU.

    Restrições:
    1) Cada RU deve ser atendida por exatamente uma DU;
    2) Distância RU-DU <= MAX_FIBER_DISTANCE_KM;
    3) Soma das larguras de banda por DU <= MAX_LOAD;
    4) Cada cluster pode ter no máximo MAX_CLUSTER_SIZE elementos, incluindo a própria DU.
    """

    # Preparar Variáveis
    NumEstacoes = df["NumEstacao"].astype(int).tolist()
    id_set = set(NumEstacoes)
    loads = df.set_index("NumEstacao")["bandwidth"].astype(float).to_dict()

    # Pares viáveis (i,j): d(ij)) <= MAX_FIBER_DISTANCE_KM
    valid_pairs = []
    distances = {} # Dicionário rápido para acesso (i, j) -> dist
    neighbors_by_i = {i: [] for i in NumEstacoes}
    incoming_by_j = {j: [] for j in NumEstacoes}

    for i in NumEstacoes:
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
        
    print(f"Iniciando modelagem ILP para {len(NumEstacoes)} estações...")
    n_valid_pairs = len(valid_pairs)
    print(f"Número de pares válidos (i,j) para conexão: {n_valid_pairs}")

    # --- 2. Criação do Modelo  ---
    m = ConcreteModel("ICARUS_Secundario")

    m.I = Set(initialize=NumEstacoes, ordered=True)
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

    #adicionado a restrição de número mínimo de DUs ao modelo encontrado no objetivo primario
    m.FixDUCount = Constraint(
        expr=sum(m.y[j] for j in m.I) == best_du_count
    )
    msg = ""

    #adicionar o objetivo secundário
    if objective_mode == "opex_capex":
        msg = "Minimizando a soma total das distâncias RU-DU."
        m.Stage2OBJ = Objective(
            expr=sum(
                m.dist[i, j] * m.x[i, j]
                for (i, j) in m.A
            ),
            sense=minimize
        )

    elif objective_mode == "cpu_power":
        msg = "Minimizando o desbalanceamento total da carga entre as O-DUs."

        total_load = float(sum(loads.values()))
        target_avg_load = total_load / float(best_du_count)

        print(f"Carga total: {total_load:.2f}")
        print(f"Carga média alvo por O-DU: {target_avg_load:.2f}")
        m.Dev = Var(m.I, domain=NonNegativeReals)

        # Linearização do valor absoluto: Dev[j] >= |Carga Real[j] - Carga Ideal * y[j]|
        def dev_pos_rule(mm, j):
            load_j = sum(mm.ru_load[i] * mm.x[i, j] for i in incoming_by_j[j])
            return mm.Dev[j] >= load_j - target_avg_load * mm.y[j]

        def dev_neg_rule(mm, j):
            load_j = sum(mm.ru_load[i] * mm.x[i, j] for i in incoming_by_j[j])
            return mm.Dev[j] >= target_avg_load * mm.y[j] - load_j

        m.DevPosConstraint = Constraint(m.I, rule=dev_pos_rule)
        m.DevNegConstraint = Constraint(m.I, rule=dev_neg_rule)

        m.Stage2OBJ = Objective(
            expr=sum(m.Dev[j] for j in m.I) + 0.001 * sum(m.dist[i, j] * m.x[i, j] for (i, j) in m.A),
            sense=minimize
        )

    # Configurações do SOLVER
    SOLVER = 'cbc'  # 'cbc', 'glpk', 'gurobi', 'cplex'
    MAX_SOLVER_THREADS = 32
    THREADS = min(os.cpu_count() or 4, MAX_SOLVER_THREADS)

    print(f"{msg} com solver {SOLVER} (threads={THREADS})...")
    print(f"Tempo limite {TIME_LIMIT_SOLVER} segundos)...")

    opt = SolverFactory(SOLVER)
    opt.options.clear()
    if objective_mode == "cpu_power":
        opt.options["ratio"] = 0.05 
    else:
        opt.options["ratio"] = 0.0001
    opt.options["threads"] = int(THREADS)
    match SOLVER:
        case "cbc":
            opt.options["seconds"] = int(TIME_LIMIT_SOLVER)
            opt.options["timeMode"] = "elapsed"
            opt.options["heuristics"] = "on" 
            opt.options["cuts"] = "on"
            opt.options["preprocess"] = "on"
        case "glpk":
            opt.options["tmlim"] = int(TIME_LIMIT_SOLVER)
        case "gurobi" | "gurobi_direct" | "gurobi_persistent":
            opt.options["TimeLimit"] = float(TIME_LIMIT_SOLVER)
        case "cplex" | "cplex_direct" | "cplex_persistent":
            opt.options["timelimit"] = float(TIME_LIMIT_SOLVER)
                
    
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

    for i in NumEstacoes:
        chosen = None
        for j in neighbors_by_i[i]:
            xv = value(m.x[i, j])
            if xv is not None and xv > 0.5:
                chosen = j
                break
        if chosen is None:
            raise RuntimeError(f"Não foi possível extrair atribuição viável para a RU {i}.")
        assignment[i] = int(chosen)

    df["O-DU"] = df["NumEstacao"].astype(int).map(assignment)
    return df

def stats(df, df_dm, output_filename: Path):
    #calcula, imprime e salva estatísticas básicas
    rows: List[Dict[str, Any]] = []

    # --- Distâncias RU->DU ---
    dist_ru_du = []
    for ru_id, du_id in zip(df["NumEstacao"].to_list(), df["O-DU"].to_list()):
        d = float(df_dm.at[ru_id, du_id])
        dist_ru_du.append(d)

    dist_ru_du = np.array(dist_ru_du, dtype=float)

    # Total enlace: soma das distâncias RU->DU (DUs contribuem com 0)
    total_enlace_km = float(dist_ru_du.sum())    

    # Média/DP das distâncias RU->DU
    # Se você NÃO quiser que DUs (0 km) puxem a média para baixo, exclua ru_id==du_id:
    dist_links = dist_ru_du[(df["NumEstacao"].values != df["O-DU"].values)]  # só RU realmente “ligadas” a uma DU distinta
    media_dist_ru_du = float(dist_links.mean())
    dp_dist_ru_du = float(dist_links.std(ddof=1))

    # --- Quantidade de DUs / clusters ---
    qtde_dus = int(df["O-DU"].nunique())

    # --- RUs por DU ---
    rus_por_du = df.groupby("O-DU")["NumEstacao"].count().astype(int)   # inclui a própria DU
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
    #abrindo o arquivo de dados de entrada (RUs + DUs) para clusterização
    csv_path = OUT_DIR / f"grp_{Filename}.csv"
    print(f"Carregando o arquivo {csv_path}")
    df = pd.read_csv(csv_path)

    #Removendo colunas desnecessárias
    df.drop(columns=['N_Latitude','N_Longitude','Latitudes','Longitudes','N_Designacoes','Designacoes','N_Setores','Setores'], inplace=True)
    df["NumEstacao"] = df["NumEstacao"].astype(int)
    csv_path = OUT_DIR / f"dm_{Filename}.csv"
    if not os.path.exists(csv_path):
        raise FileNotFoundError(f"Matriz de distâncias não encontrada: {csv_path}")
    # Carrega a Matriz de Distâncias. Define 'NumEstacao' como índice para acesso rápido: dists.loc[NumEstacao_origem, NumEstacao_destino]
    df_dm = pd.read_csv(csv_path, index_col='NumEstacao')
    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    #Executando a clusterização ILP
    best_du_count = cluster_ilp_primario(df, df_dm)
    df_cluster = cluster_ilp_secundario(df, df_dm, best_du_count, objective_mode="opex_capex")
    output_filename = OUT_DIR / f"ilp_{Filename}_opex_capex.csv"
    print(f"Gravando o resultado da clusterização em {output_filename}")
    df_cluster.to_csv(output_filename, index=False)

    #ou. se já tiver sido gerado em execução anterior, pode ser carregado diretamente:
    #print(f"Carregando clusterização de {output_filename}")
    #df_cluster = pd.read_csv(output_filename)

    #gravando as estatísticas em um arquivo CSV
    stats(df_cluster, df_dm, output_filename=OUT_DIR / f"stats_{Filename}_opex_capex.csv")

    #checando regras do cluster foram respeitadas
    check_rules(df_cluster, df_dm)
    
    df_cluster = cluster_ilp_secundario(df, df_dm, best_du_count, objective_mode="cpu_power")
    output_filename = OUT_DIR / f"ilp_{Filename}_cpu_power.csv"
    print(f"Gravando o resultado da clusterização em {output_filename}")
    df_cluster.to_csv(output_filename, index=False)

    #ou. se já tiver sido gerado em execução anterior, pode ser carregado diretamente:
    #print(f"Carregando clusterização de {output_filename}")
    #df_cluster = pd.read_csv(output_filename)
    
    #gravando as estatísticas em um arquivo CSV
    stats(df_cluster, df_dm, output_filename=OUT_DIR / f"stats_{Filename}_cpu_power.csv")

    #checando regras do cluster foram respeitadas
    check_rules(df_cluster, df_dm)
    

