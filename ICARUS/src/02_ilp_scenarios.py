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
TIME_LIMIT_SOLVER = int(os.environ.get("TIME_LIMIT_SOLVER", 1800))
MAX_SOLVER_THREADS = int(os.environ.get("MAX_SOLVER_THREADS", 4))


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
        # Aceita gap absoluto de 1 unidade, pois o objetivo é inteiro (quantidade de O-DUs). O gap relativo é zero, garantindo que a solução ótima seja provada.
        opt.options["MIPGapAbs"] = 0.999
        opt.options["MIPGap"] = 0.06
        # Aceita solução interia imediatamente acima do limite inferior, mesmo que não seja ótima, para acelerar a convergência.
        #opt.options["MIPGapAbs"] = 1.001
        #opt.options["MIPGap"] = 0.0

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
    print(f"Melhor quantidade de O-DUs encontrada dentro da tolerância configurada: {best_du_count}")

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
      - ``adversarial``:
      - ``adversarial``:
          1) maximiza a quantidade de O-DUs cuja composição foi alterada;
          2) maximiza a quantidade de O-RUs realocadas;
          3) maximiza a quantidade de níveis distintos de fanout utilizados;
          4) minimiza a distância total preservando exatamente os melhores valores encontrados nas passagens anteriores.
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

        original_members_by_du = {
            j: {
                int(i)
                for i, original_j in initial_assignment.items()
                if int(original_j) == j
            }
            for j in optimized_dus
        }

        # CompositionChanged[j] = 1 se ao menos uma O-RU saiu da composição
        # original da O-DU j ou uma nova O-RU passou a integrá-la.
        modelo.CompositionChanged = Var(modelo.OptimizedDUs, domain=Binary)

        def composition_mismatches_rule(mm, j):
            original_members = original_members_by_du[j]
            removed_rus = sum(
                1 - mm.x[i, j]
                for i in original_members
            )
            added_rus = sum(
                mm.x[i, j]
                for i in dados.incoming_by_j[j]
                if i not in original_members
            )
            return removed_rus + added_rus

        modelo.CompositionMismatches = Expression(
            modelo.OptimizedDUs,
            rule=composition_mismatches_rule,
        )
        modelo.ActivateCompositionChanged = Constraint(
            modelo.OptimizedDUs,
            rule=lambda mm, j: mm.CompositionMismatches[j]
            >= mm.CompositionChanged[j],
        )
        modelo.ChangeOnlyIfCompositionDiffers = Constraint(
            modelo.OptimizedDUs,
            rule=lambda mm, j: mm.CompositionMismatches[j]
            <= 2 * MAX_CLUSTER_SIZE * mm.CompositionChanged[j],
        )

        modelo.ChangedDUCount = Expression(
            expr=sum(
                modelo.CompositionChanged[j] for j in modelo.OptimizedDUs
            )
        )
        modelo.ReassignedRUCount = Expression(
            expr=sum(
                1 - modelo.x[i, int(initial_assignment[i])]
                for i in dados.num_estacoes
            )
        )

        # ExactFanout[j,k] = 1 se a O-DU j atende exatamente k O-RUs.
        # UsedFanoutLevel[k] = 1 se ao menos uma O-DU utiliza o nível k.
        modelo.FanoutLevels = Set(
            initialize=range(1, MAX_CLUSTER_SIZE + 1),
            ordered=True,
        )
        modelo.ExactFanout = Var(
            modelo.OptimizedDUs,
            modelo.FanoutLevels,
            domain=Binary,
        )
        modelo.UsedFanoutLevel = Var(modelo.FanoutLevels, domain=Binary)

        modelo.SelectOneFanout = Constraint(
            modelo.OptimizedDUs,
            rule=lambda mm, j: sum(
                mm.ExactFanout[j, k] for k in mm.FanoutLevels
            ) == 1,
        )
        modelo.LinkExactFanout = Constraint(
            modelo.OptimizedDUs,
            rule=lambda mm, j: sum(
                mm.x[i, j] for i in dados.incoming_by_j[j]
            ) == sum(
                k * mm.ExactFanout[j, k] for k in mm.FanoutLevels
            ),
        )
        modelo.ActivateUsedFanoutLevel = Constraint(
            modelo.OptimizedDUs,
            modelo.FanoutLevels,
            rule=lambda mm, j, k: mm.UsedFanoutLevel[k]
            >= mm.ExactFanout[j, k],
        )
        modelo.UseOnlyExistingFanoutLevel = Constraint(
            modelo.FanoutLevels,
            rule=lambda mm, k: mm.UsedFanoutLevel[k]
            <= sum(mm.ExactFanout[j, k] for j in mm.OptimizedDUs),
        )

        modelo.DistinctFanoutLevels = Expression(
            expr=sum(
                modelo.UsedFanoutLevel[k] for k in modelo.FanoutLevels
            )
        )

        modelo.TotalDistance = Expression(
            expr=sum(
                modelo.dist[i, j] * modelo.x[i, j]
                for i, j in modelo.A
            )
        )

        modelo.Stage2OBJ = Objective(
            expr=modelo.ChangedDUCount,
            sense=maximize,
        )
        
        msg = ("Executando cenário adversarial maximizando O-DUs alteradas, O-RUs realocadas e diversidade de fanout, antes de minimizar a distância total.")

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
        aplicar_valores_iniciais(modelo, dados, initial_assignment)

    ok_terms = {
        TerminationCondition.optimal,
        TerminationCondition.maxTimeLimit,
        TerminationCondition.feasible,
    }

    print(f"{msg}")
    print(f"Tempo limite por passagem: {TIME_LIMIT_SOLVER} segundos...")

    opt, threads = criar_solver(
        etapa="secundaria",
        objective_mode=objective_mode,
    )

    print(f"{msg} com solver gurobi (threads={threads})...")
    print(f"Tempo limite {TIME_LIMIT_SOLVER} segundos...")

    if objective_mode == "otimizado":
        opt, threads = criar_solver(
            etapa="secundaria",
            objective_mode="otimizado",
        )

        print(f"Solver gurobi (threads={threads})...")

        inicio = time.perf_counter()
        results = opt.solve(
            modelo,
            tee=True,
            warmstart=initial_assignment is not None,
        )
        fim = time.perf_counter()

        term = results.solver.termination_condition
        print(
            f"Tempo de resolução segunda etapa: "
            f"{fim - inicio:.2f} segundos."
        )
        print(f"TerminationCondition: {term}.")

        if term not in ok_terms:
            raise RuntimeError(
                f"Gurobi terminou em estado não aceito: {term}."
            )

    else:
        # Passagem 1: maximização da quantidade de O-DUs alteradas.
        opt_1, threads = criar_solver(
            etapa="secundaria",
            objective_mode="adversarial",
        )

        opt_1.options["MIPGap"] = 0.0

        print(
            "\n--- Passagem adversarial 1/4: "
            "maximização da quantidade de O-DUs alteradas ---"
        )
        print(f"Solver gurobi (threads={threads})...")

        inicio_1 = time.perf_counter()
        results_1 = opt_1.solve(
            modelo,
            tee=True,
            warmstart=True,
        )
        fim_1 = time.perf_counter()

        term_1 = results_1.solver.termination_condition
        print(
            f"Tempo da passagem 1: "
            f"{fim_1 - inicio_1:.2f} segundos."
        )
        print(f"TerminationCondition passagem 1: {term_1}.")

        if term_1 not in ok_terms:
            raise RuntimeError(
                "Gurobi terminou a passagem 1 em estado não aceito: "
                f"{term_1}."
            )

        best_changed_dus = int(
            round(value(modelo.ChangedDUCount))
        )

        first_pass_assignment = extrair_atribuicao(
            modelo,
            dados,
        )
        aplicar_valores_iniciais(
            modelo,
            dados,
            first_pass_assignment,
        )

        modelo.Stage2OBJ.deactivate()

        modelo.KeepBestChangedDUCount = Constraint(
            expr=modelo.ChangedDUCount == best_changed_dus
        )

        # Passagem 2: maximização das O-RUs realocadas, preservando a maior
        # quantidade encontrada de O-DUs alteradas.
        modelo.ReassignedRUOBJ = Objective(
            expr=modelo.ReassignedRUCount,
            sense=maximize,
        )

        print(
            "\n--- Passagem adversarial 2/4: "
            "maximização da quantidade de O-RUs realocadas ---"
        )
        print(
            "Maior quantidade de O-DUs alteradas encontrada e preservada: "
            f"{best_changed_dus}"
        )

        opt_2, threads_2 = criar_solver(
            etapa="secundaria",
            objective_mode="adversarial",
        )
        opt_2.options["MIPGap"] = 0.0

        print(f"Solver gurobi (threads={threads_2})...")

        inicio_2 = time.perf_counter()
        results_2 = opt_2.solve(
            modelo,
            tee=True,
            warmstart=True,
        )
        fim_2 = time.perf_counter()

        term_2 = results_2.solver.termination_condition
        print(
            f"Tempo da passagem 2: "
            f"{fim_2 - inicio_2:.2f} segundos."
        )
        print(f"TerminationCondition passagem 2: {term_2}.")

        if term_2 not in ok_terms:
            raise RuntimeError(
                "Gurobi terminou a passagem 2 em estado não aceito: "
                f"{term_2}."
            )

        best_reassigned_rus = int(
            round(value(modelo.ReassignedRUCount))
        )
        second_pass_assignment = extrair_atribuicao(modelo, dados)
        aplicar_valores_iniciais(modelo, dados, second_pass_assignment)

        modelo.ReassignedRUOBJ.deactivate()
        modelo.KeepBestReassignedRUCount = Constraint(
            expr=modelo.ReassignedRUCount == best_reassigned_rus
        )

        # Passagem 3: maximização da diversidade de fanout.
        modelo.FanoutDiversityOBJ = Objective(
            expr=modelo.DistinctFanoutLevels,
            sense=maximize,
        )

        print(
            "\n--- Passagem adversarial 3/4: "
            "maximização da diversidade de fanout ---"
        )
        print(
            "Maior quantidade de O-RUs realocadas encontrada e preservada: "
            f"{best_reassigned_rus}"
        )

        opt_3, threads_3 = criar_solver(
            etapa="secundaria",
            objective_mode="adversarial",
        )
        opt_3.options["MIPGap"] = 0.0
        print(f"Solver gurobi (threads={threads_3})...")

        inicio_3 = time.perf_counter()
        results_3 = opt_3.solve(modelo, tee=True, warmstart=True)
        fim_3 = time.perf_counter()
        term_3 = results_3.solver.termination_condition
        print(f"Tempo da passagem 3: {fim_3 - inicio_3:.2f} segundos.")
        print(f"TerminationCondition passagem 3: {term_3}.")

        if term_3 not in ok_terms:
            raise RuntimeError(
                "Gurobi terminou a passagem 3 em estado não aceito: "
                f"{term_3}."
            )

        best_distinct_fanout_levels = int(
            round(value(modelo.DistinctFanoutLevels))
        )
        third_pass_assignment = extrair_atribuicao(modelo, dados)
        aplicar_valores_iniciais(modelo, dados, third_pass_assignment)

        modelo.FanoutDiversityOBJ.deactivate()
        modelo.KeepBestFanoutDiversity = Constraint(
            expr=modelo.DistinctFanoutLevels
            == best_distinct_fanout_levels
        )

        # Passagem 4: distância como último desempate lexicográfico.
        modelo.DistanceOBJ = Objective(
            expr=modelo.TotalDistance,
            sense=minimize,
        )

        print(
            "\n--- Passagem adversarial 4/4: "
            "minimização da distância total ---"
        )
        print(
            "Maior quantidade de níveis distintos de fanout encontrada "
            f"e preservada: {best_distinct_fanout_levels}"
        )

        opt_4, threads_4 = criar_solver(
            etapa="secundaria",
            objective_mode="otimizado",
        )
        print(f"Solver gurobi (threads={threads_4})...")

        inicio_4 = time.perf_counter()
        results_4 = opt_4.solve(modelo, tee=True, warmstart=True)
        fim_4 = time.perf_counter()
        term_4 = results_4.solver.termination_condition
        print(f"Tempo da passagem 4: {fim_4 - inicio_4:.2f} segundos.")
        print(f"TerminationCondition passagem 4: {term_4}.")

        if term_4 not in ok_terms:
            raise RuntimeError(
                "Gurobi terminou a passagem 4 em estado não aceito: "
                f"{term_4}."
            )

        print(
            "Quantidade final de O-DUs com composição alterada: "
            f"{int(round(value(modelo.ChangedDUCount)))}"
        )
        print(
            "Quantidade final de O-RUs realocadas: "
            f"{int(round(value(modelo.ReassignedRUCount)))}"
        )
        print(
            "Quantidade final de níveis distintos de fanout: "
            f"{int(round(value(modelo.DistinctFanoutLevels)))}"
        )
        print(
            "Distância total final: "
            f"{value(modelo.TotalDistance):.6f} km"
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
    df_otimizado = cluster_ilp_secundario(df,df_dm,best_du_count,objective_mode="otimizado",initial_assignment=primary_assignment,)
    optimized_output = OUT_DIR / f"ilp_{Filename}_otimizado.csv"
    print(f"Gravando o cenário otimizado em {optimized_output}")
    df_otimizado.to_csv(optimized_output, index=False)
    #df_otimizado = pd.read_csv(optimized_output)
    
    #Cenário adversarial maior dispersão das cargas agregadas por O-DU mantenado as posições encontradas no cenário otimizado
    df_adversarial = cluster_ilp_secundario(df,df_dm,best_du_count,objective_mode="adversarial",initial_assignment=(df_otimizado.assign(NumEstacao=df_otimizado["NumEstacao"].astype(int),**{"O-DU": df_otimizado["O-DU"].astype(int)},).set_index("NumEstacao")["O-DU"].to_dict())    )
    adversarial_output = OUT_DIR / f"ilp_{Filename}_adversarial.csv"
    print(f"Gravando o cenário adversarial em {adversarial_output}")
    df_adversarial.to_csv(adversarial_output, index=False)
