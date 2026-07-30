#!/bin/bash
set -Eeuo pipefail

# Executor das topologias do ICARUS.
#
# Cada chamada de run_topology.sh recebe somente uma linha do pipeline, isto é,
# uma única O-DU e suas O-RUs. A unidade de execução passa a ser:
#
#   roundtrip + cenário + O-DU
#
# Isso impede que uma falha no meio de um arquivo deixe as demais O-DUs daquele
# cenário sem execução sem que o controlador saiba exatamente onde ocorreu.
#
# Variáveis opcionais no .env ou no ambiente:
#   ROUNDTRIPS=30
#   MAX_TENTATIVAS_PRE_KPI=2
#
# MAX_TENTATIVAS_PRE_KPI controla somente repetições seguras, isto é, quando
# get_gnbemu_kpi.py ainda não foi iniciado. Uma falha durante a coleta não é
# repetida automaticamente, pois pode ter ocorrido gravação parcial no SQLite.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../.env"
RUN_TOPOLOGY="${SCRIPT_DIR}/run_topology.sh"

fatal() {
    echo "ERRO: $*" >&2
    exit 1
}

info() {
    echo "$*"
}

require_command() {
    command -v "$1" >/dev/null 2>&1 || fatal "comando obrigatório não encontrado: $1"
}

cleanup_temp_file() {
    if [[ -n "${TEMP_PIPELINE_ATUAL:-}" && -f "${TEMP_PIPELINE_ATUAL}" ]]; then
        rm -f -- "${TEMP_PIPELINE_ATUAL}"
    fi
}

on_interrupt() {
    cleanup_temp_file
    echo >&2
    echo "Execução interrompida. Os checkpoints concluídos foram preservados." >&2
    echo "Execute novamente o mesmo script para retomar a campanha." >&2
    exit 130
}

trap cleanup_temp_file EXIT
trap on_interrupt INT TERM

[[ ${EUID} -eq 0 ]] || fatal "este script deve ser executado como root"
[[ -f "$ENV_FILE" ]] || fatal "arquivo .env não encontrado: $ENV_FILE"
[[ -f "$RUN_TOPOLOGY" ]] || fatal "run_topology.sh não encontrado: $RUN_TOPOLOGY"

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

: "${Filename:?A variável Filename deve estar definida no .env}"
: "${OUT_DIR:?A variável OUT_DIR deve estar definida no .env}"

ROUNDTRIPS="${ROUNDTRIPS:-1}"
MAX_TENTATIVAS_PRE_KPI="${MAX_TENTATIVAS_PRE_KPI:-2}"

[[ "$ROUNDTRIPS" =~ ^[0-9]+$ ]] || fatal "ROUNDTRIPS deve ser um número inteiro"
(( ROUNDTRIPS >= 1 && ROUNDTRIPS <= 30 )) || fatal "ROUNDTRIPS deve estar entre 1 e 30"

[[ "$MAX_TENTATIVAS_PRE_KPI" =~ ^[0-9]+$ ]] || \
    fatal "MAX_TENTATIVAS_PRE_KPI deve ser um número inteiro"
(( MAX_TENTATIVAS_PRE_KPI >= 1 )) || \
    fatal "MAX_TENTATIVAS_PRE_KPI deve ser maior ou igual a 1"

require_command realpath
require_command sha256sum
require_command mktemp
require_command tee
require_command grep
require_command flock
require_command awk

# Mantém a mesma interpretação de OUT_DIR utilizada por run_topology.sh,
# mas resolve o caminho relativamente ao diretório dos scripts, e não ao
# diretório corrente de quem chamou o executor.
FULL_PATH="$(cd "$SCRIPT_DIR" && realpath -m "../.${OUT_DIR}")"
[[ -d "$FULL_PATH" ]] || fatal "diretório de saída não encontrado: $FULL_PATH"

PIPELINE_ADVERSARIAL="${FULL_PATH}/pipeline_${Filename}_adversarial.txt"
PIPELINE_OTIMIZADO="${FULL_PATH}/pipeline_${Filename}_otimizado.txt"

[[ -f "$PIPELINE_ADVERSARIAL" ]] || fatal "pipeline adversarial não encontrado: $PIPELINE_ADVERSARIAL"
[[ -f "$PIPELINE_OTIMIZADO" ]] || fatal "pipeline otimizado não encontrado: $PIPELINE_OTIMIZADO"

STATE_DIR="${FULL_PATH}/.pipeline_state/${Filename}"
DONE_DIR="${STATE_DIR}/done"
LOG_DIR="${STATE_DIR}/logs"
TMP_DIR="${STATE_DIR}/tmp"
MANIFEST_FILE="${STATE_DIR}/manifest.txt"
LOCK_FILE="${STATE_DIR}/execution.lock"

mkdir -p "$DONE_DIR" "$LOG_DIR" "$TMP_DIR"

# Impede duas campanhas simultâneas usando o mesmo banco, namespaces e portas.
exec 9>"$LOCK_FILE"
flock -n 9 || fatal "já existe outra execução ativa para o identificador ${Filename}"

ADV_SHA256="$(sha256sum "$PIPELINE_ADVERSARIAL" | awk '{print $1}')"
OTI_SHA256="$(sha256sum "$PIPELINE_OTIMIZADO" | awk '{print $1}')"

CURRENT_MANIFEST="$(cat <<MANIFEST
filename=${Filename}
adversarial_sha256=${ADV_SHA256}
otimizado_sha256=${OTI_SHA256}
MANIFEST
)"

if [[ -f "$MANIFEST_FILE" ]]; then
    SAVED_MANIFEST="$(cat "$MANIFEST_FILE")"
    if [[ "$SAVED_MANIFEST" != "$CURRENT_MANIFEST" ]]; then
        fatal "os arquivos pipeline foram alterados desde o início da campanha. Remova conscientemente '${STATE_DIR}' para iniciar uma campanha nova"
    fi
else
    printf '%s\n' "$CURRENT_MANIFEST" > "$MANIFEST_FILE"

    # O banco é arquivado somente na inicialização da campanha. Em uma retomada,
    # ele não pode ser movido, pois contém as coletas já concluídas.
    if [[ -f "${FULL_PATH}/icarus.db" ]]; then
        BACKUP_DB="${FULL_PATH}/icarus_$(date +'%Y%m%d_%H%M%S').db"
        info "Armazenando o banco anterior como: $BACKUP_DB"
        mv -- "${FULL_PATH}/icarus.db" "$BACKUP_DB"
    else
        info "Nenhum banco anterior encontrado em ${FULL_PATH}/icarus.db"
    fi
fi

declare -A LINHAS_ADVERSARIAL=()
declare -A LINHAS_OTIMIZADO=()
declare -a ORDEM_DUS=()

load_pipeline() {
    local arquivo="$1"
    local nome_cenario="$2"
    local mapa_nome="$3"
    local registrar_ordem="$4"
    local -n mapa_ref="$mapa_nome"

    local linha=""
    local linha_sem_espacos=""
    local du_id=""
    local numero_linha=0
    local -a campos=()

    while IFS= read -r linha || [[ -n "$linha" ]]; do
        numero_linha=$((numero_linha + 1))
        linha="${linha//$'\r'/}"
        linha_sem_espacos="$(printf '%s' "$linha" | tr -d '[:space:]')"

        [[ -z "$linha_sem_espacos" ]] && continue
        [[ "$linha" =~ ^[[:space:]]*# ]] && continue

        IFS=',' read -r -a campos <<< "$linha"

        (( ${#campos[@]} >= 3 )) || \
            fatal "${nome_cenario}: linha ${numero_linha} possui menos de três campos"

        (( ${#campos[@]} % 3 == 0 )) || \
            fatal "${nome_cenario}: linha ${numero_linha} não está organizada em trios"

        du_id="$(printf '%s' "${campos[0]}" | tr -d '[:space:]')"
        [[ "$du_id" =~ ^[0-9]{9,10}$ ]] || \
            fatal "${nome_cenario}: DU_ID inválido na linha ${numero_linha}: ${du_id}"

        [[ -z "${mapa_ref[$du_id]+x}" ]] || \
            fatal "${nome_cenario}: DU_ID duplicado no pipeline: ${du_id}"

        mapa_ref["$du_id"]="$linha"

        if [[ "$registrar_ordem" == "1" ]]; then
            ORDEM_DUS+=("$du_id")
        fi
    done < "$arquivo"

    (( ${#mapa_ref[@]} > 0 )) || fatal "pipeline ${nome_cenario} não possui linhas executáveis"
}

load_pipeline "$PIPELINE_ADVERSARIAL" "adversarial" LINHAS_ADVERSARIAL 1
load_pipeline "$PIPELINE_OTIMIZADO" "otimizado" LINHAS_OTIMIZADO 0

(( ${#LINHAS_ADVERSARIAL[@]} == ${#LINHAS_OTIMIZADO[@]} )) || \
    fatal "os pipelines possuem quantidades diferentes de O-DUs: adversarial=${#LINHAS_ADVERSARIAL[@]}, otimizado=${#LINHAS_OTIMIZADO[@]}"

for du_id in "${ORDEM_DUS[@]}"; do
    [[ -n "${LINHAS_OTIMIZADO[$du_id]+x}" ]] || \
        fatal "a O-DU ${du_id} existe no adversarial, mas não no otimizado"
done

for du_id in "${!LINHAS_OTIMIZADO[@]}"; do
    [[ -n "${LINHAS_ADVERSARIAL[$du_id]+x}" ]] || \
        fatal "a O-DU ${du_id} existe no otimizado, mas não no adversarial"
done

TOTAL_DUS="${#ORDEM_DUS[@]}"
TOTAL_UNIDADES=$((ROUNDTRIPS * TOTAL_DUS * 2))

checkpoint_path() {
    local roundtrip="$1"
    local cenario="$2"
    local du_id="$3"
    printf '%s/roundtrip_%02d_%s_%s.done' "$DONE_DIR" "$roundtrip" "$cenario" "$du_id"
}

count_checkpoints() {
    local cenario="$1"
    local total=0
    local roundtrip du_id checkpoint

    for ((roundtrip=1; roundtrip<=ROUNDTRIPS; roundtrip++)); do
        for du_id in "${ORDEM_DUS[@]}"; do
            checkpoint="$(checkpoint_path "$roundtrip" "$cenario" "$du_id")"
            [[ -f "$checkpoint" ]] && total=$((total + 1))
        done
    done

    printf '%s' "$total"
}

write_checkpoint() {
    local checkpoint="$1"
    local roundtrip="$2"
    local cenario="$3"
    local du_id="$4"
    local resultado="$5"
    local tmp_checkpoint="${checkpoint}.tmp.$$"

    cat > "$tmp_checkpoint" <<CHECKPOINT
roundtrip=${roundtrip}
cenario=${cenario}
clusterid=${du_id}
resultado=${resultado}
concluido_em=$(date -u '+%Y-%m-%dT%H:%M:%SZ')
CHECKPOINT

    mv -- "$tmp_checkpoint" "$checkpoint"
}

execute_unit() {
    local roundtrip="$1"
    local cenario="$2"
    local du_id="$3"
    local linha="$4"

    local checkpoint
    local round_log_dir
    local tentativa=1
    local log_file=""
    local status=0
    local kpi_iniciado=0
    local kpi_concluido=0
    local topologia_removida=0

    checkpoint="$(checkpoint_path "$roundtrip" "$cenario" "$du_id")"

    if [[ -f "$checkpoint" ]]; then
        info "[IGNORADO] roundtrip=${roundtrip} cenário=${cenario} O-DU=${du_id}: checkpoint já existente"
        return 0
    fi

    round_log_dir="${LOG_DIR}/roundtrip_$(printf '%02d' "$roundtrip")"
    mkdir -p "$round_log_dir"

    while (( tentativa <= MAX_TENTATIVAS_PRE_KPI )); do
        TEMP_PIPELINE_ATUAL="$(mktemp "${TMP_DIR}/${cenario}_${du_id}_XXXXXX.txt")"
        printf '%s\n' "$linha" > "$TEMP_PIPELINE_ATUAL"

        log_file="${round_log_dir}/${cenario}_${du_id}_tentativa_$(printf '%02d' "$tentativa").log"

        echo
        echo "============================================================"
        echo "Roundtrip:  $roundtrip de $ROUNDTRIPS"
        echo "Cenário:    $cenario"
        echo "O-DU:       $du_id"
        echo "Tentativa:  $tentativa de $MAX_TENTATIVAS_PRE_KPI"
        echo "Log:        $log_file"
        echo "============================================================"

        set +e
        (
            cd "$SCRIPT_DIR"
            bash "$RUN_TOPOLOGY" \
                "$TEMP_PIPELINE_ATUAL" \
                "$roundtrip" \
                "$cenario" \
                "$Filename"
        ) 2>&1 | tee "$log_file"
        status=${PIPESTATUS[0]}
        set -e

        rm -f -- "$TEMP_PIPELINE_ATUAL"
        TEMP_PIPELINE_ATUAL=""

        # O run_topology atual usa "continue 2" em algumas falhas de startup.
        # Nesses casos ele pode encerrar com status zero sem coletar métricas.
        # Por isso o status do processo não é usado isoladamente.
        if grep -Fq "get_gnbemu_kpi.py --seconds" "$log_file" && \
           grep -Fq -- "--clusterid ${du_id} --roundtrip ${roundtrip} --cenario ${cenario}" "$log_file"; then
            kpi_iniciado=1
        else
            kpi_iniciado=0
        fi

        # Esta seção somente é impressa depois que get_gnbemu_kpi.py retorna zero.
        if grep -Fq "Estatísticas qdisc após a coleta" "$log_file"; then
            kpi_concluido=1
        else
            kpi_concluido=0
        fi

        if grep -Fq "Topologia da GNB/DU ${du_id} removida." "$log_file"; then
            topologia_removida=1
        else
            topologia_removida=0
        fi

        if (( kpi_concluido == 1 )); then
            if (( status == 0 && topologia_removida == 1 )); then
                write_checkpoint "$checkpoint" "$roundtrip" "$cenario" "$du_id" "sucesso"
                info "[CONCLUÍDO] roundtrip=${roundtrip} cenário=${cenario} O-DU=${du_id}"
            else
                # As métricas foram coletadas. Não repetir, mesmo que qdisc,
                # encerramento de processo ou limpeza posterior tenham falhado.
                write_checkpoint "$checkpoint" "$roundtrip" "$cenario" "$du_id" \
                    "metricas_coletadas_com_falha_posterior_status_${status}"
                echo "AVISO: métricas concluídas, mas houve falha posterior à coleta." >&2
                echo "  Roundtrip: $roundtrip" >&2
                echo "  Cenário:   $cenario" >&2
                echo "  O-DU:      $du_id" >&2
                echo "  Status:    $status" >&2
                echo "  Log:       $log_file" >&2
                echo "A unidade foi marcada como concluída para evitar duplicação." >&2
            fi
            return 0
        fi

        if (( kpi_iniciado == 1 )); then
            # O Python foi iniciado, porém não há confirmação de término. Uma
            # repetição automática poderia duplicar uma gravação parcial.
            echo "ERRO: a coleta foi iniciada, mas não há confirmação de conclusão." >&2
            echo "  Roundtrip: $roundtrip" >&2
            echo "  Cenário:   $cenario" >&2
            echo "  O-DU:      $du_id" >&2
            echo "  Status:    $status" >&2
            echo "  Log:       $log_file" >&2
            echo "A campanha foi interrompida para evitar duplicação no banco." >&2
            return 1
        fi

        echo "AVISO: falha ocorreu antes do início da coleta de métricas." >&2
        echo "  Roundtrip: $roundtrip" >&2
        echo "  Cenário:   $cenario" >&2
        echo "  O-DU:      $du_id" >&2
        echo "  Status:    $status" >&2
        echo "  Log:       $log_file" >&2

        if (( tentativa < MAX_TENTATIVAS_PRE_KPI )); then
            echo "Repetindo esta unidade, pois ainda não houve coleta..." >&2
            sleep 5
        fi

        tentativa=$((tentativa + 1))
    done

    echo "ERRO: número máximo de tentativas atingido antes da coleta." >&2
    echo "  Roundtrip: $roundtrip" >&2
    echo "  Cenário:   $cenario" >&2
    echo "  O-DU:      $du_id" >&2
    echo "  Último log: $log_file" >&2
    return 1
}

CONCLUIDOS_ADV_INICIAIS="$(count_checkpoints adversarial)"
CONCLUIDOS_OTI_INICIAIS="$(count_checkpoints otimizado)"

if (( CONCLUIDOS_ADV_INICIAIS + CONCLUIDOS_OTI_INICIAIS > 0 )) && \
   [[ ! -f "${FULL_PATH}/icarus.db" ]]; then
    fatal "existem checkpoints concluídos, mas o banco ${FULL_PATH}/icarus.db não existe; restaure o banco correspondente antes de retomar"
fi

cat <<SUMMARY

============================================================
INÍCIO DA CAMPANHA
============================================================
Diretório de saída:       $FULL_PATH
Identificador:             $Filename
Roundtrips:                $ROUNDTRIPS
O-DUs por cenário:         $TOTAL_DUS
Unidades totais esperadas: $TOTAL_UNIDADES
Checkpoints adversarial:   $CONCLUIDOS_ADV_INICIAIS
Checkpoints otimizado:     $CONCLUIDOS_OTI_INICIAIS
Estado da campanha:        $STATE_DIR
============================================================
SUMMARY

# Os cenários são executados em pares para cada O-DU. A ordem fixa preserva o
# comportamento anterior, no qual o arquivo adversarial aparecia antes do
# otimizado na ordenação lexical.
CENARIOS=(adversarial otimizado)

for ((roundtrip=1; roundtrip<=ROUNDTRIPS; roundtrip++)); do
    echo
    echo "############################################################"
    echo "INICIANDO ROUNDTRIP $roundtrip DE $ROUNDTRIPS"
    echo "############################################################"

    for du_id in "${ORDEM_DUS[@]}"; do
        for cenario in "${CENARIOS[@]}"; do
            if [[ "$cenario" == "adversarial" ]]; then
                linha="${LINHAS_ADVERSARIAL[$du_id]}"
            else
                linha="${LINHAS_OTIMIZADO[$du_id]}"
            fi

            if ! execute_unit "$roundtrip" "$cenario" "$du_id" "$linha"; then
                echo >&2
                echo "ROUNDTRIP $roundtrip INTERROMPIDO COM ERRO." >&2
                echo "A campanha poderá ser retomada sem repetir as unidades com checkpoint." >&2
                exit 1
            fi
        done
    done

    echo
    echo "ROUNDTRIP $roundtrip CONCLUÍDO NOS DOIS CENÁRIOS"
done

CONCLUIDOS_ADV_FINAIS="$(count_checkpoints adversarial)"
CONCLUIDOS_OTI_FINAIS="$(count_checkpoints otimizado)"
ESPERADO_POR_CENARIO=$((ROUNDTRIPS * TOTAL_DUS))

if (( CONCLUIDOS_ADV_FINAIS != ESPERADO_POR_CENARIO || \
      CONCLUIDOS_OTI_FINAIS != ESPERADO_POR_CENARIO )); then
    fatal "validação final falhou: esperado=${ESPERADO_POR_CENARIO}, adversarial=${CONCLUIDOS_ADV_FINAIS}, otimizado=${CONCLUIDOS_OTI_FINAIS}"
fi

cat <<FINAL

============================================================
CAMPANHA CONCLUÍDA COM SUCESSO
============================================================
Adversarial: $CONCLUIDOS_ADV_FINAIS execuções concluídas
Otimizado:   $CONCLUIDOS_OTI_FINAIS execuções concluídas
Diferença:   0
Checkpoints: $DONE_DIR
Logs:        $LOG_DIR
============================================================
FINAL
