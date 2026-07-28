import math
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import pandas as pd
from dotenv import find_dotenv, load_dotenv
from pyomo.environ import (
    Binary,
    ConcreteModel,
    Constraint,
    Expression,
    NonNegativeReals,
    Objective,
    Param,
    Set,
    Var,
    maximize,
    minimize,
    value,
)
from pyomo.opt import SolverFactory, TerminationCondition


load_dotenv(find_dotenv())
Filename = os.environ["Filename"]
OUT_DIR = Path(os.environ["OUT_DIR"]).resolve()
MAX_CLUSTER_SIZE = int(os.environ["MAX_RUS"])
MAX_FIBER_DISTANCE_KM = float(os.environ["MAX_FIBER_DISTANCE_KM"])
MAX_LOAD = int(os.environ["MAX_LOAD"])
TIME_LIMIT_SOLVER = int(os.environ.get("TIME_LIMIT_SOLVER", 3200))
MAX_SOLVER_THREADS = int(os.environ.get("MAX_SOLVER_THREADS", 32))


@dataclass(frozen=True)
class DadosModeloILP:
    """Estruturas auxiliares compartilhadas pelas duas etapas lexicográficas."""

    num_estacoes: list[int]
    loads: dict[int, float]
    valid_pairs: list[tuple[int, int]]
    distances: dict[tuple[int, int], float]
    neighbors_by_i: dict[int, list[int]]
    incoming_by_j: dict[int, list[int]]


def preparar_dados_modelo(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
) -> DadosModeloILP:
    """Prepara cargas, pares RU-DU viáveis e listas de adjacência."""
    num_estacoes = df["NumEstacao"].astype(int).tolist()
    id_set = set(num_estacoes)
    loads = (
        df.assign(NumEstacao=df["NumEstacao"].astype(int))
        .set_index("NumEstacao")["bandwidth"]
        .astype(float)
        .to_dict()
    )

    valid_pairs: list[tuple[int, int]] = []
    distances: dict[tuple[int, int], float] = {}
    neighbors_by_i = {i: [] for i in num_estacoes}
    incoming_by_j = {j: [] for j in num_estacoes}

    for i in num_estacoes:
        if i not in df_dm.index:
            raise KeyError(f"A estação {i} não existe no índice da matriz de distâncias.")

        dists_i = df_dm.loc[i]
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
            pair = (i, j)
            valid_pairs.append(pair)
            distances[pair] = float(dists_i[j])
            neighbors_by_i[i].append(j)
            incoming_by_j[j].append(i)

    return DadosModeloILP(
        num_estacoes=num_estacoes,
        loads=loads,
        valid_pairs=valid_pairs,
        distances=distances,
        neighbors_by_i=neighbors_by_i,
        incoming_by_j=incoming_by_j,
    )


def criar_modelo_base(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
    nome_modelo: str,
) -> tuple[ConcreteModel, DadosModeloILP]:
    """
    Cria a parte comum dos modelos primário e secundário.

    Inclui conjuntos, parâmetros, variáveis e as restrições de associação,
    capacidade, autoassociação e cardinalidade. As restrições e os objetivos
    específicos de cada etapa são adicionados pelas funções chamadoras.
    """
    dados = preparar_dados_modelo(df, df_dm)

    print(f"Iniciando modelagem ILP para {len(dados.num_estacoes)} estações...")
    print(
        "Critério de distância máxima para pares RU-DU: "
        f"{MAX_FIBER_DISTANCE_KM} km"
    )
    print(f"Critério de carga máxima por DU: {MAX_LOAD} MHz")
    print(
        "Critério de tamanho máximo de cluster: "
        f"{MAX_CLUSTER_SIZE} RUs (incluindo a própria DU)"
    )
    print(
        "Número de pares válidos (i,j) para conexão: "
        f"{len(dados.valid_pairs)}"
    )

    modelo = ConcreteModel(nome_modelo)

    modelo.I = Set(initialize=dados.num_estacoes, ordered=True)
    modelo.A = Set(dimen=2, initialize=dados.valid_pairs, ordered=False)

    modelo.ru_load = Param(
        modelo.I,
        initialize=lambda mm, i: float(dados.loads[i]),
        within=NonNegativeReals,
        mutable=False,
    )
    modelo.dist = Param(
        modelo.A,
        initialize=lambda mm, i, j: float(dados.distances[(i, j)]),
        within=NonNegativeReals,
        mutable=False,
    )

    # y[j] = 1 se a estação j for ativada como O-DU.
    modelo.y = Var(modelo.I, domain=Binary)
    # x[i,j] = 1 se a O-RU i for atendida pela O-DU j.
    modelo.x = Var(modelo.A, domain=Binary)

    def assign_rule(mm, i):
        return sum(mm.x[i, j] for j in dados.neighbors_by_i[i]) == 1

    modelo.Assign = Constraint(modelo.I, rule=assign_rule)

    def capacity_rule(mm, j):
        return (
            sum(
                mm.ru_load[i] * mm.x[i, j]
                for i in dados.incoming_by_j[j]
            )
            <= float(MAX_LOAD) * mm.y[j]
        )

    modelo.Capacity = Constraint(modelo.I, rule=capacity_rule)

    def self_assignment_rule(mm, j):
        return mm.x[j, j] == mm.y[j]

    modelo.SelfAssignment = Constraint(modelo.I, rule=self_assignment_rule)

    def max_cluster_size_rule(mm, j):
        return (
            sum(mm.x[i, j] for i in dados.incoming_by_j[j])
            <= MAX_CLUSTER_SIZE * mm.y[j]
        )

    modelo.MaxClusterSize = Constraint(
        modelo.I,
        rule=max_cluster_size_rule,
    )

    return modelo, dados


def aplicar_valores_iniciais(
    modelo: ConcreteModel,
    dados: DadosModeloILP,
    assignment: dict[int, int],
) -> None:
    """Define os valores iniciais de x e y para o warm start."""
    dus = set(assignment.values())

    for j in dados.num_estacoes:
        modelo.y[j].value = 1 if j in dus else 0

    for i, j in dados.valid_pairs:
        modelo.x[i, j].value = 1 if assignment.get(i) == j else 0


def extrair_atribuicao(
    modelo: ConcreteModel,
    dados: DadosModeloILP,
) -> dict[int, int]:
    """Extrai do modelo resolvido o mapeamento O-RU -> O-DU."""
    assignment: dict[int, int] = {}

    for i in dados.num_estacoes:
        chosen = None
        for j in dados.neighbors_by_i[i]:
            x_value = value(modelo.x[i, j])
            if x_value is not None and x_value > 0.5:
                chosen = int(j)
                break

        if chosen is None:
            raise RuntimeError(
                f"Não foi possível extrair uma atribuição viável para a RU {i}."
            )

        assignment[i] = chosen

    return assignment


def criar_solver(
    etapa: str,
    objective_mode: Optional[str] = None,
):
    """
    Na etapa primária, prioriza o fechamento do gap e utiliza gap absoluto
    inferior a uma unidade, adequado ao objetivo inteiro de quantidade de
    O-DUs. Na etapa secundária, mantém configuração distinta para o MILP
    otimizado e para maximização de desvio absoluto total adversarial.
    """
    threads = min(os.cpu_count() or 4, MAX_SOLVER_THREADS)
    opt = SolverFactory("gurobi")

    if not opt.available(exception_flag=False):
        raise RuntimeError(
            "O solver Gurobi não está disponível. Verifique a instalação, "
            "o PATH e a licença acadêmica."
        )

    opt.options.clear()
    opt.options["Threads"] = int(threads)
    opt.options["TimeLimit"] = float(TIME_LIMIT_SOLVER)

    if etapa == "primaria":
        # O objetivo é inteiro. Uma diferença absoluta menor que 1 prova
        # qual é o menor valor inteiro possível para a quantidade de O-DUs.
        opt.options["MIPGapAbs"] = 0.999
        opt.options["MIPGap"] = 0.0

        # Prioriza a melhoria do best bound e a prova de otimalidade.
        opt.options["MIPFocus"] = 2

    elif etapa == "secundaria":
        if objective_mode == "otimizado":
            # MILP linear: minimização da soma das distâncias.
            opt.options["MIPGap"] = 0.0001

        elif objective_mode == "adversarial":
            opt.options["MIPGap"] = 0.05
            opt.options["MIPFocus"] = 1
        else:
            raise ValueError(
                "objective_mode deve ser 'otimizado' ou 'adversarial'; "
                f"recebido: {objective_mode!r}."
            )
    else:
        raise ValueError(f"Etapa de solução desconhecida: {etapa!r}.")

    return opt, threads


def cluster_ilp_primario(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
) -> tuple[int, dict[int, int]]:
    """
    Executa a primeira etapa lexicográfica.

    O objetivo é encontrar e provar a menor quantidade viável de O-DUs.
    """
    modelo, dados = criar_modelo_base(
        df,
        df_dm,
        nome_modelo="ICARUS_Primario",
    )

    lower_bound_fanout = math.ceil(
        len(dados.num_estacoes) / MAX_CLUSTER_SIZE
    )
    lower_bound_capacity = math.ceil(
        sum(dados.loads.values()) / MAX_LOAD
    )
    lower_bound_du = max(lower_bound_fanout, lower_bound_capacity)

    modelo.DULowerBound = Constraint(
        expr=sum(modelo.y[j] for j in modelo.I) >= lower_bound_du
    )

    print(
        "Limite inferior simples para o número de O-DUs: "
        f"{lower_bound_du} "
        f"(fanout={lower_bound_fanout}, "
        f"capacidade={lower_bound_capacity})."
    )

    modelo.Stage1OBJ = Objective(
        expr=sum(modelo.y[j] for j in modelo.I),
        sense=minimize,
    )

    opt, threads = criar_solver(etapa="primaria")

    print(
        "Minimizando o número de DUs com solver "
        f"gurobi (threads={threads})..."
    )
    print(f"Tempo limite {TIME_LIMIT_SOLVER} segundos...")

    inicio = time.perf_counter()
    results = opt.solve(modelo, tee=True)
    fim = time.perf_counter()

    print(f"Tempo de resolução primeira etapa: {fim - inicio:.2f} segundos.")
    term = results.solver.termination_condition
    print(f"TerminationCondition: {term}.")

    if term != TerminationCondition.optimal:
        raise RuntimeError(
            "A minimização do número de DUs não provou otimalidade. "
            f"TerminationCondition: {term}. Para garantir o número mínimo "
            "de DUs, a primeira etapa precisa ser ótima."
        )

    best_du_count = int(
        round(sum(value(modelo.y[j]) for j in modelo.I))
    )
    print(f"Número mínimo de DUs encontrado: {best_du_count}")

    optimal_assignment = extrair_atribuicao(modelo, dados)
    return best_du_count, optimal_assignment


def cluster_ilp_secundario(
    df: pd.DataFrame,
    df_dm: pd.DataFrame,
    best_du_count: int,
    objective_mode: str,
    initial_assignment: Optional[dict[int, int]] = None,
) -> pd.DataFrame:
    """
    Executa a segunda etapa lexicográfica com o número de O-DUs fixado.

    Modos:
      - ``otimizado``: minimiza a soma total das distâncias O-RU--O-DU;
      - ``adversarial``: maximiza o desvio absoluto total das
        O-DUs já ativadas.
    """
    modelo, dados = criar_modelo_base(
        df,
        df_dm,
        nome_modelo=f"ICARUS_Secundario_{objective_mode}",
    )

    modelo.FixDUCount = Constraint(
        expr=sum(modelo.y[j] for j in modelo.I) == best_du_count
    )

    # Expressão compartilhada: carga agregada atendida pela candidata j.
    def du_load_rule(mm, j):
        return sum(
            mm.ru_load[i] * mm.x[i, j]
            for i in dados.incoming_by_j[j]
        )

    modelo.DULoad = Expression(modelo.I, rule=du_load_rule)

    total_load = float(sum(dados.loads.values()))
    average_active_du_load = total_load / float(best_du_count)

    if objective_mode == "otimizado":
        msg = "Minimizando a soma total das distâncias RU-DU."

        modelo.Stage2OBJ = Objective(
            expr=sum(
                modelo.dist[i, j] * modelo.x[i, j]
                for i, j in modelo.A
            ),
            sense=minimize,
        )

    elif objective_mode == "adversarial":
        optimized_dus = {int(du) for du in initial_assignment.values()}

        # Mantém exatamente as mesmas localizações de O-DU do cenário otimizado.
        def fix_optimized_dus_rule(mm, j):
            return mm.y[j] == (1 if j in optimized_dus else 0)

        modelo.FixOptimizedDUs = Constraint(
            modelo.I,
            rule=fix_optimized_dus_rule,
        )

        modelo.OptimizedDUs = Set(
            initialize=sorted(optimized_dus),
            ordered=True,
        )

        modelo.LoadDeviation = Var(
            modelo.OptimizedDUs,
            domain=NonNegativeReals,
        )

        modelo.AboveMean = Var(
            modelo.OptimizedDUs,
            domain=Binary,
        )

        big_m = float(MAX_LOAD)

        def dev_lower_positive_rule(mm, j):
            return (
                mm.LoadDeviation[j]
                >= mm.DULoad[j] - average_active_du_load
            )

        def dev_lower_negative_rule(mm, j):
            return (
                mm.LoadDeviation[j]
                >= average_active_du_load - mm.DULoad[j]
            )

        def dev_upper_positive_rule(mm, j):
            return (
                mm.LoadDeviation[j]
                <= mm.DULoad[j]
                - average_active_du_load
                + big_m * (1 - mm.AboveMean[j])
            )

        def dev_upper_negative_rule(mm, j):
            return (
                mm.LoadDeviation[j]
                <= average_active_du_load
                - mm.DULoad[j]
                + big_m * mm.AboveMean[j]
            )

        modelo.DevLowerPositive = Constraint(
            modelo.OptimizedDUs,
            rule=dev_lower_positive_rule,
        )
        modelo.DevLowerNegative = Constraint(
            modelo.OptimizedDUs,
            rule=dev_lower_negative_rule,
        )
        modelo.DevUpperPositive = Constraint(
            modelo.OptimizedDUs,
            rule=dev_upper_positive_rule,
        )
        modelo.DevUpperNegative = Constraint(
            modelo.OptimizedDUs,
            rule=dev_upper_negative_rule,
        )

        modelo.TotalAbsoluteDeviation = Expression(
            expr=sum(
                modelo.LoadDeviation[j]
                for j in modelo.OptimizedDUs
            )
        )

        modelo.TotalDistance = Expression(
            expr=sum(
                modelo.dist[i, j] * modelo.x[i, j]
                for i, j in modelo.A
            )
        )

        modelo.Stage2OBJ = Objective(
            expr=modelo.TotalAbsoluteDeviation,
            sense=maximize,
        )
        
        msg = (
            "Maximizando a variância da carga agregada entre as "
            "O-DUs ativadas."
        )

        print(f"Carga total das O-RUs: {total_load:.2f} MHz")
        print(
            "Carga média entre as O-DUs ativadas: "
            f"{average_active_du_load:.2f} MHz"
        )

        print(
            "O-DUs fixadas pelo cenário otimizado: "
            f"{len(optimized_dus)}"
        )

    else:
        raise ValueError(
            "objective_mode deve ser 'otimizado' ou 'adversarial'; "
            f"recebido: {objective_mode!r}."
        )

    if initial_assignment is not None:
        assignment_keys = set(initial_assignment)
        expected_keys = set(dados.num_estacoes)

        if assignment_keys != expected_keys:
            missing = sorted(expected_keys - assignment_keys)
            extra = sorted(assignment_keys - expected_keys)
            raise ValueError(
                "O initial_assignment deve conter exatamente uma atribuição "
                "para cada O-RU. "
                f"Ausentes: {missing[:5]}; extras: {extra[:5]}."
            )

        initial_du_count = len(set(initial_assignment.values()))
        if initial_du_count != best_du_count:
            raise ValueError(
                "O warm start utiliza quantidade de O-DUs diferente da "
                "fixada na segunda etapa: "
                f"{initial_du_count} != {best_du_count}."
            )

        for i, j in initial_assignment.items():
            if j not in dados.neighbors_by_i[i]:
                raise ValueError(
                    f"Warm start inválido: a associação ({i}, {j}) "
                    "não respeita o conjunto de pares viáveis."
                )

        aplicar_valores_iniciais(modelo, dados, initial_assignment)

    opt, threads = criar_solver(
        etapa="secundaria",
        objective_mode=objective_mode,
    )

    print(f"{msg} com solver gurobi (threads={threads})...")
    print(f"Tempo limite {TIME_LIMIT_SOLVER} segundos...")

    inicio = time.perf_counter()
    results = opt.solve(
        modelo,
        tee=True,
        warmstart=initial_assignment is not None,
    )
    fim = time.perf_counter()

    print(f"Tempo de resolução segunda etapa: {fim - inicio:.2f} segundos.")
    term = results.solver.termination_condition
    print(f"TerminationCondition: {term}.")

    ok_terms = {
        TerminationCondition.optimal,
        TerminationCondition.maxTimeLimit,
        TerminationCondition.feasible,
    }
    if term not in ok_terms:
        raise RuntimeError(
            f"Gurobi terminou em estado não aceito: {term}; "
            "nenhum resultado será exportado."
        )

    # Em maxTimeLimit, a extração também funciona como verificação de que
    # existe um incumbente inteiro carregado no modelo.
    assignment = extrair_atribuicao(modelo, dados)

    active_dus = sorted(set(assignment.values()))
    if len(active_dus) != best_du_count:
        raise RuntimeError(
            "A solução extraída não respeita a quantidade fixa de O-DUs: "
            f"{len(active_dus)} != {best_du_count}."
        )

    if objective_mode == "adversarial":
        du_loads = {
            j: sum(
                dados.loads[i]
                for i, assigned_j in assignment.items()
                if assigned_j == j
            )
            for j in active_dus
        }

        mean_load = sum(du_loads.values()) / len(du_loads)
        variance = sum(
            (load - mean_load) ** 2
            for load in du_loads.values()
        ) / len(du_loads)
        standard_deviation = math.sqrt(variance)

        print(
            "Carga agregada das O-DUs: "
            f"mínima={min(du_loads.values()):.2f} MHz; "
            f"máxima={max(du_loads.values()):.2f} MHz; "
            f"média={mean_load:.2f} MHz; "
            f"desvio padrão={standard_deviation:.2f} MHz."
        )

    result_df = df.copy()
    result_df["O-DU"] = (
        result_df["NumEstacao"].astype(int).map(assignment)
    )

    if result_df["O-DU"].isna().any():
        missing_rows = result_df.loc[
            result_df["O-DU"].isna(),
            "NumEstacao",
        ].tolist()
        raise RuntimeError(
            "Algumas O-RUs não receberam O-DU no resultado: "
            f"{missing_rows[:10]}."
        )

    result_df["O-DU"] = result_df["O-DU"].astype(int)
    return result_df


if __name__ == "__main__":
    # Abre os dados de entrada das O-RUs candidatas à clusterização.
    csv_path = OUT_DIR / f"grp_{Filename}.csv"
    print(f"Carregando o arquivo {csv_path}")
    df = pd.read_csv(csv_path)

    df.drop(
        columns=[
            "N_Latitude",
            "N_Longitude",
            "Latitudes",
            "Longitudes",
            "N_Designacoes",
            "Designacoes",
            "N_Setores",
            "Setores",
        ],
        inplace=True,
    )
    df["NumEstacao"] = df["NumEstacao"].astype(int)

    csv_path = OUT_DIR / f"dm_{Filename}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Matriz de distâncias não encontrada: {csv_path}"
        )

    df_dm = pd.read_csv(csv_path, index_col="NumEstacao")
    df_dm.index = df_dm.index.astype(int)
    df_dm.columns = df_dm.columns.astype(int)

    best_du_count, primary_assignment = cluster_ilp_primario(df, df_dm)

    # Cenário otimizado: menor soma das distâncias RU-DU.
    df_otimizado = cluster_ilp_secundario(
        df,
        df_dm,
        best_du_count,
        objective_mode="otimizado",
        initial_assignment=primary_assignment,
    )
    optimized_output = OUT_DIR / f"ilp_{Filename}_otimizado.csv"
    print(f"Gravando o cenário otimizado em {optimized_output}")
    df_otimizado.to_csv(optimized_output, index=False)

    # Cenário adversarial maior dispersão das cargas agregadas por O-DU mantenado as posições encontradas no cenário otimizado
    #df_adversarial = cluster_ilp_secundario(df,df_dm,best_du_count,objective_mode="adversarial",initial_assignment=(df_otimizado.assign(NumEstacao=df_otimizado["NumEstacao"].astype(int),**{"O-DU": df_otimizado["O-DU"].astype(int)},).set_index("NumEstacao")["O-DU"].to_dict())    )
    #adversarial_output = OUT_DIR / f"ilp_{Filename}_adversarial.csv"
    #print(f"Gravando o cenário adversarial em {adversarial_output}")
    #df_adversarial.to_csv(adversarial_output, index=False)
