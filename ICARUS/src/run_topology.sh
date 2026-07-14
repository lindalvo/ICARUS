#!/bin/bash
set -euo pipefail

# Uso:
#   rodar como root
#   ./pipeline.sh clusters.txt
#
# Arquivos de matriz:
#   ru_matriz.yml
#   GNB_1RU_matriz.yml
#   GNB_2RU_matriz.yml
#   GNB_3RU_matriz.yml
#   GNB_4RU_matriz.yml
#   GNB_5RU_matriz.yml
#
# Formato esperado por linha:
#   DU_ID,BW_DU,0,BW_RU1,delay_us,BW_RU2,delay_us,...
#
# Exemplo:
#   7,20,0,20,16,20,4,20,7,20,1

if [ -z "${1:-}" ]; then
    echo "Uso: $0 <nome_do_arquivo.txt> <iteracao> <cenario> <identificador>"
    exit 1
fi

ARQUIVO="$1"

if [ ! -f "$ARQUIVO" ]; then
    echo "Erro: arquivo '$ARQUIVO' não encontrado."
    exit 1
fi

if [ -z "${2:-}" ]; then
    echo "Erro: O segundo parâmetro (iteração) é obrigatório."
    echo "Uso: $0 <arquivo> <1-99> <max_link|total_distance> <identificador>"
    exit 1
fi

ROUNDTRIP="$2"

# Verifica se é um número inteiro positivo entre 1 e 30
if [[ ! "$ROUNDTRIP" =~ ^[0-9]+$ ]] ; then
    echo "Erro: O roundtrip '$ROUNDTRIP' deve ser um número inteiro"
    exit 1
fi

if [ -z "${3:-}" ]; then
    echo "Erro: O terceiro parâmetro (cenário) é obrigatório."
    echo "Uso: $0 <arquivo> <1-99> <max_link|total_distance> <identificador>"
    exit 1
fi

CENARIO="$3"

# Verifica se o valor é exatamente uma das duas opções permitidas
if [ "$CENARIO" != "max_link" ] && [ "$CENARIO" != "total_distance" ]; then
    echo "Erro: O parâmetro '$CENARIO' é inválido. Escolha entre 'max_link' ou 'total_distance'."
    exit 1
fi

if [ -z "${4:-}" ]; then
    echo "Erro: O quarto parâmetro (identificador) é obrigatório."
    echo "Uso: $0 <arquivo> <1-99> <max_link|total_distance> <identificador>"
    exit 1
fi

IDENTIFICADOR="$4"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../.env"
set -a             
source "$ENV_FILE"
set +a

RU_MATRIZ="${SCRIPT_DIR}/../assets/ru_matriz.yml"

VCPUS_TOTAL=48

echo "========================================"
echo "Arquivo de entrada: $ARQUIVO"
echo "Diretório do script: $SCRIPT_DIR"
echo "Matriz YAML das RUs: $RU_MATRIZ"
echo "Matrizes YAML da GNB: ${SCRIPT_DIR}/../assets/GNB_<N>RU_matriz.yml"
echo "Diretório base de saída: ${OUT_DIR}"
echo "vCPUs totais consideradas no script: $VCPUS_TOTAL"
echo "Housekeeping reservado: CPUs 0-1"
echo "CPUs OpenRAN disponíveis: 2-47"
echo "NUMA node0: CPUs 0-23"
echo "NUMA node1: CPUs 24-47"
echo "MTU fixo: $MTU"
echo "txqueuelen fixo: $TXQLEN"
echo "Máximo de RUs por GNB/DU: $MAX_RUS"
echo "ARFCNs fixos:"
echo "  RU1: $RU1_DL_ARFCN"
echo "  RU2: $RU2_DL_ARFCN"
echo "  RU3: $RU3_DL_ARFCN"
echo "  RU4: $RU4_DL_ARFCN"
echo "  RU5: $RU5_DL_ARFCN"
echo "========================================"
echo ""

echo "+ modprobe sch_netem"
modprobe sch_netem 2>/dev/null || true
echo ""

STARTUP_TIMEOUT=180
CHECK_INTERVAL=1
LINHA_NUM=0
METRICS_HOST=127.0.0.1
METRICS_PORT=8001

while IFS= read -r linha || [ -n "$linha" ]; do
    LINHA_NUM=$((LINHA_NUM + 1))

    linha="${linha//$'\r'/}"

    linha_sem_espaco="$(echo "$linha" | tr -d '[:space:]')"

    if [ -z "$linha_sem_espaco" ]; then
        echo "Linha $LINHA_NUM ignorada: linha vazia ou contendo apenas espaços."
        echo ""
        continue
    fi

    if [[ "$linha" =~ ^[[:space:]]*# ]]; then
        echo "Linha $LINHA_NUM ignorada: comentário."
        echo "Conteúdo: $linha"
        echo ""
        continue
    fi

    IFS=',' read -r -a elementos <<< "$linha"

    if [ "${#elementos[@]}" -lt 3 ]; then
        echo "Erro na linha $LINHA_NUM: quantidade insuficiente de campos."
        echo "Linha: $linha"
        exit 1
    fi

    if [ $(( ${#elementos[@]} % 2 )) -eq 0 ]; then
        echo "Erro na linha $LINHA_NUM: a linha deve ter DU_ID seguido de pares BW,Delay."
        echo "Linha: $linha"
        exit 1
    fi

    DU_ID_RAW="$(echo "${elementos[0]}" | tr -d '[:space:]')"

    DU_ID="$(awk -v v="$DU_ID_RAW" 'BEGIN {
        if (v !~ /^[0-9]+([.][0-9]+)?$/) exit 1;
        printf "%.0f", v;
    }')" || {
        echo "Erro na linha $LINHA_NUM: DU_ID inválido: $DU_ID_RAW"
        exit 1
    }

    CONFIG_DIR="../.${OUT_DIR}/${IDENTIFICADOR}/${CENARIO}/${ROUNDTRIP}/gnb_${DU_ID}"
    GNB_YAML="${CONFIG_DIR}/gnb_${DU_ID}.yml"
	GNB_OUTPUT="${CONFIG_DIR}/gnb_${DU_ID}.out"

    echo "============================================================"
    echo "Linha $LINHA_NUM"
    echo "GNB/DU ID: $DU_ID"
    echo "Conteúdo: $linha"
    echo "Diretório dos YAMLs: $CONFIG_DIR"
    echo "============================================================"
    echo ""

    echo "+ mkdir -p $CONFIG_DIR"
    mkdir -p "$CONFIG_DIR"
    echo ""

    echo "Limpando processos Zumbis"
    for pid in $(pgrep -f 'gnb'); do
       echo "Encerrando PID GNB : $pid"
       kill -9 "$pid" 2>/dev/null || true
    done
    echo "Limpando processos Zumbis"

    for pid in $(pgrep -f 'ru_emulator'); do
       echo "Encerrando PID RU_EMULATOR : $pid"
       kill -9 "$pid" 2>/dev/null || true
    done

    echo "Limpando possível topologia anterior..."
    for ((j=1; j<=MAX_RUS; j++)); do
        NS="ru${j}"
        DU_IF="ofh_du${j}"

        if ip netns list | awk '{print $1}' | grep -qx "$NS"; then
            echo "+ ip netns del $NS"
            ip netns del "$NS"
        else
            echo "- namespace $NS não existe"
        fi

        if ip link show "$DU_IF" >/dev/null 2>&1; then
            echo "+ ip link del $DU_IF"
            ip link del "$DU_IF"
        else
            echo "- interface $DU_IF não existe"
        fi
    done

    echo ""
    echo "Montando topologia virtual para GNB/DU $DU_ID..."
    echo ""

    DU_IF1=""
    DU_IF2=""
    DU_IF3=""
    DU_IF4=""
    DU_IF5=""

    RU_IF1=""
    RU_IF2=""
    RU_IF3=""
    RU_IF4=""
    RU_IF5=""

    DU_MAC1=""
    DU_MAC2=""
    DU_MAC3=""
    DU_MAC4=""
    DU_MAC5=""

    RU1_MAC=""
    RU2_MAC=""
    RU3_MAC=""
    RU4_MAC=""
    RU5_MAC=""

    RU1_BANDWIDTH_MHZ=""
    RU2_BANDWIDTH_MHZ=""
    RU3_BANDWIDTH_MHZ=""
    RU4_BANDWIDTH_MHZ=""
    RU5_BANDWIDTH_MHZ=""

    RU1_DL_PORT_ID=""
    RU2_DL_PORT_ID=""
    RU3_DL_PORT_ID=""
    RU4_DL_PORT_ID=""
    RU5_DL_PORT_ID=""

    RU1_UL_PORT_ID=""
    RU2_UL_PORT_ID=""
    RU3_UL_PORT_ID=""
    RU4_UL_PORT_ID=""
    RU5_UL_PORT_ID=""

    RU1_PRACH_PORT_ID=""
    RU2_PRACH_PORT_ID=""
    RU3_PRACH_PORT_ID=""
    RU4_PRACH_PORT_ID=""
    RU5_PRACH_PORT_ID=""

    RU1_CPUSET=""
    RU2_CPUSET=""
    RU3_CPUSET=""
    RU4_CPUSET=""
    RU5_CPUSET=""

    RU1_CPUS=""
    RU2_CPUS=""
    RU3_CPUS=""
    RU4_CPUS=""
    RU5_CPUS=""

    RU1_CPUS_MASK=""
    RU2_CPUS_MASK=""
    RU3_CPUS_MASK=""
    RU4_CPUS_MASK=""
    RU5_CPUS_MASK=""

    CONT=0

    for ((i=1; i<${#elementos[@]}; i+=2)); do
        CONT=$((CONT + 1))

        if [ "$CONT" -gt "$MAX_RUS" ]; then
            echo "Erro: a linha $LINHA_NUM tem mais de $MAX_RUS RUs."
            exit 1
        fi

        RU_BW_RAW="$(echo "${elementos[i]}" | tr -d '[:space:]')"
        DELAY_RAW="$(echo "${elementos[i+1]}" | tr -d '[:space:]')"

        if [ -z "$RU_BW_RAW" ]; then
            echo "Erro na linha $LINHA_NUM: largura de banda vazia na RU $CONT."
            exit 1
        fi

        if [ -z "$DELAY_RAW" ]; then
            echo "Erro na linha $LINHA_NUM: delay vazio na RU $CONT."
            exit 1
        fi

        DELAY_US="$(awk -v v="$DELAY_RAW" 'BEGIN {
            if (v !~ /^[0-9]+([.][0-9]+)?$/) exit 1;
            printf "%.0f", v;
        }')" || {
            echo "Erro na linha $LINHA_NUM: delay inválido: $DELAY_RAW"
            exit 1
        }

        NS="ru${CONT}"
        DU_IF="ofh_du${CONT}"
        RU_IF="ofh_ru${CONT}"

        DU_HIGH_HEX="$(printf "%02x" $(( (DU_ID / 256) & 255 )))"
        DU_LOW_HEX="$(printf "%02x" $(( DU_ID & 255 )))"
        RU_IDX_HEX="$(printf "%02x" "$CONT")"

        DU_MAC="02:${DU_HIGH_HEX}:${DU_LOW_HEX}:d0:00:${RU_IDX_HEX}"
        RU_MAC="02:${DU_HIGH_HEX}:${DU_LOW_HEX}:e0:00:${RU_IDX_HEX}"

        DL_PORT_ID=$((CONT - 1))
        UL_PORT_ID=$((CONT - 1))
        PRACH_PORT_ID=$((CONT + 9))

        RU_INDEX="$CONT"
        RU_ID="du${DU_ID}_ru${RU_INDEX}"
        RU_YAML="${CONFIG_DIR}/ru${RU_INDEX}.yml"

        if [ "$CONT" -eq 1 ]; then
            DU_IF1="$DU_IF"
            RU_IF1="$RU_IF"
            DU_MAC1="$DU_MAC"
            RU1_MAC="$RU_MAC"
            RU1_BANDWIDTH_MHZ="$RU_BW_RAW"
            RU1_DL_PORT_ID="$DL_PORT_ID"
            RU1_UL_PORT_ID="$UL_PORT_ID"
            RU1_PRACH_PORT_ID="$PRACH_PORT_ID"
        elif [ "$CONT" -eq 2 ]; then
            DU_IF2="$DU_IF"
            RU_IF2="$RU_IF"
            DU_MAC2="$DU_MAC"
            RU2_MAC="$RU_MAC"
            RU2_BANDWIDTH_MHZ="$RU_BW_RAW"
            RU2_DL_PORT_ID="$DL_PORT_ID"
            RU2_UL_PORT_ID="$UL_PORT_ID"
            RU2_PRACH_PORT_ID="$PRACH_PORT_ID"
        elif [ "$CONT" -eq 3 ]; then
            DU_IF3="$DU_IF"
            RU_IF3="$RU_IF"
            DU_MAC3="$DU_MAC"
            RU3_MAC="$RU_MAC"
            RU3_BANDWIDTH_MHZ="$RU_BW_RAW"
            RU3_DL_PORT_ID="$DL_PORT_ID"
            RU3_UL_PORT_ID="$UL_PORT_ID"
            RU3_PRACH_PORT_ID="$PRACH_PORT_ID"
        elif [ "$CONT" -eq 4 ]; then
            DU_IF4="$DU_IF"
            RU_IF4="$RU_IF"
            DU_MAC4="$DU_MAC"
            RU4_MAC="$RU_MAC"
            RU4_BANDWIDTH_MHZ="$RU_BW_RAW"
            RU4_DL_PORT_ID="$DL_PORT_ID"
            RU4_UL_PORT_ID="$UL_PORT_ID"
            RU4_PRACH_PORT_ID="$PRACH_PORT_ID"
        elif [ "$CONT" -eq 5 ]; then
            DU_IF5="$DU_IF"
            RU_IF5="$RU_IF"
            DU_MAC5="$DU_MAC"
            RU5_MAC="$RU_MAC"
            RU5_BANDWIDTH_MHZ="$RU_BW_RAW"
            RU5_DL_PORT_ID="$DL_PORT_ID"
            RU5_UL_PORT_ID="$UL_PORT_ID"
            RU5_PRACH_PORT_ID="$PRACH_PORT_ID"
        fi

        echo "------------------------------------------------------------"
        echo "RU: $CONT"
        echo "Namespace RU: $NS"
        echo "Interface GNB/DU: $DU_IF"
        echo "Interface RU: $RU_IF"
        echo "Banda rádio RU: ${RU_BW_RAW} MHz"
        echo "Delay configurado: ${DELAY_US} us"
        echo "MAC GNB/DU: $DU_MAC"
        echo "MAC RU: $RU_MAC"
        echo "DL_PORT_ID: $DL_PORT_ID"
        echo "UL_PORT_ID: $UL_PORT_ID"
        echo "PRACH_PORT_ID: $PRACH_PORT_ID"
        echo "Arquivo YAML RU: $RU_YAML"
        echo "------------------------------------------------------------"

        echo "+ ip netns add $NS"
        ip netns add "$NS"

        echo "+ ip link add $DU_IF type veth peer name $RU_IF"
        ip link add "$DU_IF" type veth peer name "$RU_IF"

        echo "+ ip link set $RU_IF netns $NS"
        ip link set "$RU_IF" netns "$NS"

        echo "+ ip link set dev $DU_IF address $DU_MAC"
        ip link set dev "$DU_IF" address "$DU_MAC"

        echo "+ ip netns exec $NS ip link set dev $RU_IF address $RU_MAC"
        ip netns exec "$NS" ip link set dev "$RU_IF" address "$RU_MAC"

        echo "+ ip link set dev $DU_IF mtu $MTU"
        ip link set dev "$DU_IF" mtu "$MTU"

        echo "+ ip netns exec $NS ip link set dev $RU_IF mtu $MTU"
        ip netns exec "$NS" ip link set dev "$RU_IF" mtu "$MTU"

        echo "+ ip link set dev $DU_IF txqueuelen $TXQLEN"
        ip link set dev "$DU_IF" txqueuelen "$TXQLEN"

        echo "+ ip netns exec $NS ip link set dev $RU_IF txqueuelen $TXQLEN"
        ip netns exec "$NS" ip link set dev "$RU_IF" txqueuelen "$TXQLEN"
		
		# Desabilitar IPV6
		echo "+ sysctl -w net.ipv6.conf.$DU_IF.disable_ipv6=1"
		sysctl -w net.ipv6.conf."$DU_IF".disable_ipv6=1
        echo "+ ip netns exec $NS sysctl -w net.ipv6.conf.$RU_IF.disable_ipv6=1"
        ip netns exec "$NS" sysctl -w net.ipv6.conf."$RU_IF".disable_ipv6=1
		
		# Desabilitar offloads
		echo "+ ethtool -K $DU_IF gro off gso off tso off rx off tx off"
		ethtool -K "$DU_IF" gro off gso off tso off rx off tx off
		echo "+ ip netns exec $NS ethtool -K $RU_IF gro off gso off tso off rx off tx off"
		ip netns exec "$NS" ethtool -K "$RU_IF" gro off gso off tso off rx off tx off
		
        echo "+ ip link set dev $DU_IF promisc on"
        ip link set dev "$DU_IF" promisc on

        echo "+ ip netns exec $NS ip link set dev $RU_IF promisc on"
        ip netns exec "$NS" ip link set dev "$RU_IF" promisc on

        echo "+ ip link set dev $DU_IF up"
        ip link set dev "$DU_IF" up

        echo "+ ip netns exec $NS ip link set dev lo up"
        ip netns exec "$NS" ip link set dev lo up

        echo "+ ip netns exec $NS ip link set dev $RU_IF up"
        ip netns exec "$NS" ip link set dev "$RU_IF" up

        if [ "$DELAY_US" -gt 0 ]; then
            echo "+ tc qdisc replace dev $DU_IF root netem delay ${DELAY_US}us"
            tc qdisc replace dev "$DU_IF" root netem delay "${DELAY_US}us"

            echo "+ ip netns exec $NS tc qdisc replace dev $RU_IF root netem delay ${DELAY_US}us"
            ip netns exec "$NS" tc qdisc replace dev "$RU_IF" root netem delay "${DELAY_US}us"
        else
            echo "- delay 0 us: nenhum qdisc netem aplicado em $DU_IF/$RU_IF"
        fi

        echo ""
        echo "Gerando YAML da RU $CONT..."
        echo "+ sed ... $RU_MATRIZ > $RU_YAML"

        sed \
            -e "s|__DU_ID__|${DU_ID}|g" \
            -e "s|__RU_INDEX__|${RU_INDEX}|g" \
            -e "s|__RU_ID__|${RU_ID}|g" \
            -e "s|__RU_BANDWIDTH_MHZ__|${RU_BW_RAW}|g" \
            -e "s|__RU_IF__|${RU_IF}|g" \
            -e "s|__RU_MAC__|${RU_MAC}|g" \
            -e "s|__DU_MAC__|${DU_MAC}|g" \
            -e "s|__DL_PORT_ID__|${DL_PORT_ID}|g" \
            -e "s|__UL_PORT_ID__|${UL_PORT_ID}|g" \
            -e "s|__PRACH_PORT_ID__|${PRACH_PORT_ID}|g" \
            "$RU_MATRIZ" > "$RU_YAML"

        echo "YAML gerado: $RU_YAML"
        echo ""
    done

    GNB_CPUSET=""
    GNB_NUMA="0,1"

    if [ "$CONT" -eq 1 ]; then
        GNB_CPUSET="2-41"
        RU1_CPUSET="42-47"
    elif [ "$CONT" -eq 2 ]; then
        GNB_CPUSET="2-39"
        RU1_CPUSET="40-43"
        RU2_CPUSET="44-47"
    elif [ "$CONT" -eq 3 ]; then
        GNB_CPUSET="2-35"
        RU1_CPUSET="36-39"
        RU2_CPUSET="40-43"
        RU3_CPUSET="44-47"
    elif [ "$CONT" -eq 4 ]; then
        GNB_CPUSET="2-31"
        RU1_CPUSET="32-35"
        RU2_CPUSET="36-39"
        RU3_CPUSET="40-43"
        RU4_CPUSET="43-47"
    elif [ "$CONT" -eq 5 ]; then
        GNB_CPUSET="2-27"
        RU1_CPUSET="28-31"
        RU2_CPUSET="32-35"
        RU3_CPUSET="36-39"
        RU4_CPUSET="40-43"
        RU5_CPUSET="44-47"
    fi

    GNB_MATRIZ="${SCRIPT_DIR}/../assets/GNB_${CONT}RU_matriz.yml"

    echo "============================================================"
    echo "Gerando YAML da GNB"
    echo "============================================================"
    echo "Quantidade de RUs atendidas: $CONT"
    echo "Matriz GNB selecionada: $GNB_MATRIZ"
    echo "Arquivo YAML GNB: $GNB_YAML"
    echo "+ sed ... $GNB_MATRIZ > $GNB_YAML"
    echo ""

    sed \
        -e "s|__DU_ID__|${DU_ID}|g" \
        -e "s|__DU_IF1__|${DU_IF1}|g" \
        -e "s|__DU_IF2__|${DU_IF2}|g" \
        -e "s|__DU_IF3__|${DU_IF3}|g" \
        -e "s|__DU_IF4__|${DU_IF4}|g" \
        -e "s|__DU_IF5__|${DU_IF5}|g" \
        -e "s|__DU_MAC1__|${DU_MAC1}|g" \
        -e "s|__DU_MAC2__|${DU_MAC2}|g" \
        -e "s|__DU_MAC3__|${DU_MAC3}|g" \
        -e "s|__DU_MAC4__|${DU_MAC4}|g" \
        -e "s|__DU_MAC5__|${DU_MAC5}|g" \
        -e "s|__RU1_MAC__|${RU1_MAC}|g" \
        -e "s|__RU2_MAC__|${RU2_MAC}|g" \
        -e "s|__RU3_MAC__|${RU3_MAC}|g" \
        -e "s|__RU4_MAC__|${RU4_MAC}|g" \
        -e "s|__RU5_MAC__|${RU5_MAC}|g" \
        -e "s|__RU1_BANDWIDTH_MHZ__|${RU1_BANDWIDTH_MHZ}|g" \
        -e "s|__RU2_BANDWIDTH_MHZ__|${RU2_BANDWIDTH_MHZ}|g" \
        -e "s|__RU3_BANDWIDTH_MHZ__|${RU3_BANDWIDTH_MHZ}|g" \
        -e "s|__RU4_BANDWIDTH_MHZ__|${RU4_BANDWIDTH_MHZ}|g" \
        -e "s|__RU5_BANDWIDTH_MHZ__|${RU5_BANDWIDTH_MHZ}|g" \
        -e "s|__RU1_DL_PORT_ID__|${RU1_DL_PORT_ID}|g" \
        -e "s|__RU2_DL_PORT_ID__|${RU2_DL_PORT_ID}|g" \
        -e "s|__RU3_DL_PORT_ID__|${RU3_DL_PORT_ID}|g" \
        -e "s|__RU4_DL_PORT_ID__|${RU4_DL_PORT_ID}|g" \
        -e "s|__RU5_DL_PORT_ID__|${RU5_DL_PORT_ID}|g" \
        -e "s|__RU1_UL_PORT_ID__|${RU1_UL_PORT_ID}|g" \
        -e "s|__RU2_UL_PORT_ID__|${RU2_UL_PORT_ID}|g" \
        -e "s|__RU3_UL_PORT_ID__|${RU3_UL_PORT_ID}|g" \
        -e "s|__RU4_UL_PORT_ID__|${RU4_UL_PORT_ID}|g" \
        -e "s|__RU5_UL_PORT_ID__|${RU5_UL_PORT_ID}|g" \
        -e "s|__RU1_PRACH_PORT_ID__|${RU1_PRACH_PORT_ID}|g" \
        -e "s|__RU2_PRACH_PORT_ID__|${RU2_PRACH_PORT_ID}|g" \
        -e "s|__RU3_PRACH_PORT_ID__|${RU3_PRACH_PORT_ID}|g" \
        -e "s|__RU4_PRACH_PORT_ID__|${RU4_PRACH_PORT_ID}|g" \
        -e "s|__RU5_PRACH_PORT_ID__|${RU5_PRACH_PORT_ID}|g" \
        -e "s|__RU1_DL_ARFCN__|${RU1_DL_ARFCN}|g" \
        -e "s|__RU2_DL_ARFCN__|${RU2_DL_ARFCN}|g" \
        -e "s|__RU3_DL_ARFCN__|${RU3_DL_ARFCN}|g" \
        -e "s|__RU4_DL_ARFCN__|${RU4_DL_ARFCN}|g" \
        -e "s|__RU5_DL_ARFCN__|${RU5_DL_ARFCN}|g" \
        -e "s|__RU1_CPUS__|${RU1_CPUS}|g" \
        -e "s|__RU2_CPUS__|${RU2_CPUS}|g" \
        -e "s|__RU3_CPUS__|${RU3_CPUS}|g" \
        -e "s|__RU4_CPUS__|${RU4_CPUS}|g" \
        -e "s|__RU5_CPUS__|${RU5_CPUS}|g" \
        -e "s|__RU1_CPUS_MASK__|${RU1_CPUS_MASK}|g" \
        -e "s|__RU2_CPUS_MASK__|${RU2_CPUS_MASK}|g" \
        -e "s|__RU3_CPUS_MASK__|${RU3_CPUS_MASK}|g" \
        -e "s|__RU4_CPUS_MASK__|${RU4_CPUS_MASK}|g" \
        -e "s|__RU5_CPUS_MASK__|${RU5_CPUS_MASK}|g" \
        "$GNB_MATRIZ" > "$GNB_YAML"

    echo "YAML da GNB gerado: $GNB_YAML"
    echo ""

    echo "============================================================"
    echo "Topologia montada para GNB/DU $DU_ID com $CONT RU(s)."
    echo "============================================================"
    echo ""

    echo "Estado dos namespaces:"
    echo "+ ip netns list"
    ip netns list
    echo ""

    echo "Estado resumido das interfaces veth no namespace raiz:"
    echo "+ ip -br link show type veth"
    ip -br link show type veth || true
    echo ""

    echo "Estado detalhado por RU:"
    for ((j=1; j<=CONT; j++)); do
        NS="ru${j}"
        DU_IF="ofh_du${j}"
        RU_IF="ofh_ru${j}"

        echo "------------------------------------------------------------"
        echo "RU $j"
        echo "------------------------------------------------------------"

        echo "+ ip -br link show $DU_IF"
        ip -br link show "$DU_IF"

        echo "+ ip netns exec $NS ip -br link show"
        ip netns exec "$NS" ip -br link show

        echo "+ tc qdisc show dev $DU_IF"
        tc qdisc show dev "$DU_IF"

        echo "+ ip netns exec $NS tc qdisc show dev $RU_IF"
        ip netns exec "$NS" tc qdisc show dev "$RU_IF"

        echo ""
    done

    echo ""
    echo "Arquivos gerados:"
    echo "  GNB: $GNB_YAML"
    for ((j=1; j<=CONT; j++)); do
        echo "  RU${j}: ${CONFIG_DIR}/ru${j}.yml"
    done

    echo "Política de CPU aplicada:"
    echo "  Sistema Operacional Linux: CPUs 0-1"
    echo "  GNB CPUs: $GNB_CPUSET"
    echo "  GNB NUMA cpunodebind/membind: $GNB_NUMA"
    echo "  RUs NUMA cpunodebind/membind: 1"
    echo ""
	#armazenando os IDs dos processos para encerrá-los ao final
    pids=()
    echo "Executando a GNB $DU_ID:"
    echo "+ numactl --cpunodebind=${GNB_NUMA} --membind=${GNB_NUMA} taskset -c ${GNB_CPUSET} gnb -c ${GNB_YAML} > ${GNB_OUTPUT} 2>&1 &"
    numactl --cpunodebind="$GNB_NUMA" --membind="$GNB_NUMA" taskset -c "$GNB_CPUSET" gnb -c "$GNB_YAML" > "$GNB_OUTPUT" 2>&1 &
    GNB_PID=$!
    pids+=("$GNB_PID")
    echo "gNB iniciada com PID $GNB_PID."
    echo "Aguardando o serviço de métricas em ${METRICS_HOST}:${METRICS_PORT} ..."
    start_time=$(date +%s)
    while true; do
      # Verifica se a porta TCP já está aceitando conexões
      if timeout 1 bash -c \
          "exec 3<>/dev/tcp/${METRICS_HOST}/${METRICS_PORT}" 2>/dev/null
      then
          echo "Serviço de métricas disponível na porta ${METRICS_PORT}."
          break
      fi

      # Verifica se a gNB encerrou durante a inicialização
      if ! kill -0 "$GNB_PID" 2>/dev/null; then
          echo "ERRO: o processo da gNB encerrou antes de disponibilizar a porta ${METRICS_PORT}." >&2
          echo "Últimas linhas do log:" >&2
          tail -n 30 "$GNB_OUTPUT" >&2
          continue 2
      fi

      current_time=$(date +%s)
      elapsed=$((current_time - start_time))

      if (( elapsed >= STARTUP_TIMEOUT )); then
          echo "ERRO: timeout após ${STARTUP_TIMEOUT}s aguardando a porta ${METRICS_PORT}." >&2
	  kill -9 $GNB_PID
          echo "Processo PID ${GNB_PID} da gNB encerrado." >&2
	  sleep 1
          echo "Últimas linhas do log:" >&2
          tail -n 30 "$GNB_OUTPUT" >&2
          continue 2
      fi

      printf '\rAguardando a porta %s... %ss/%ss' \
          "$METRICS_PORT" "$elapsed" "$STARTUP_TIMEOUT"

      sleep "$CHECK_INTERVAL"
    done
	
    echo "Executando as ${CONT} RUs Emuladas:"

    for ((j=1; j<=CONT; j++)); do
        NS="ru${j}"
        RU_YAML="${CONFIG_DIR}/ru${j}.yml"
		RU_OUTPUT="${CONFIG_DIR}/ru${j}.out"

        if [ "$j" -eq 1 ]; then
            RU_CPUSET="$RU1_CPUSET"
        elif [ "$j" -eq 2 ]; then
            RU_CPUSET="$RU2_CPUSET"
        elif [ "$j" -eq 3 ]; then
            RU_CPUSET="$RU3_CPUSET"
        elif [ "$j" -eq 4 ]; then
            RU_CPUSET="$RU4_CPUSET"
        elif [ "$j" -eq 5 ]; then
            RU_CPUSET="$RU5_CPUSET"
        fi
        echo "------------------------------------------------------------"
        echo "RU emulada $j"
        echo "+ ip netns exec ${NS} numactl --cpunodebind=1 --membind=1 taskset -c ${RU_CPUSET} ru_emulator -c ${RU_YAML} > ${RU_OUTPUT} 2>&1 &"
		ip netns exec "$NS" numactl --cpunodebind=1 --membind=1 taskset -c "$RU_CPUSET" ru_emulator -c "$RU_YAML" > "$RU_OUTPUT" 2>&1 &
		pids+=($!)
    done

    echo "============================================================"
    echo "       Executando a topologia por 90 Segundos para coleta de métricas de desempenho"           "
    echo "============================================================"
	DATA_INICIO=$(date -u '+%Y-%m-%dT%H:%M:%S.000Z')
    echo ""
    echo "Inicio: $DATA_INICIO" 
    echo "+ poetry run python ./get_gnbemu_kpi.py --seconds 180 --clusterid ${DU_ID} --roundtrip ${ROUNDTRIP} --cenario ${CENARIO} --identificador ${IDENTIFICADOR}" 
    poetry run python ./get_gnbemu_kpi.py --seconds 90 --clusterid ${DU_ID} --roundtrip ${ROUNDTRIP} --cenario ${CENARIO} --identificador ${IDENTIFICADOR}
	DATA_FIM=$(date -u '+%Y-%m-%dT%H:%M:%S.000Z')
    echo "Fim: $DATA_FIM" 
    echo ""
    echo "============================================================"
    echo "Coletando Métricas de consumo de energia da VM entre $DATA_INICIO e $DATA_FIM "
    echo "============================================================"
    echo ""
    echo "+ poetry run python ./get_power_kpi.py --start ${DATA_INICIO} --end ${DATA_FIM} --clusterid ${DU_ID} --roundtrip ${ROUNDTRIP} --cenario ${CENARIO} --identificador ${IDENTIFICADOR}" 
    poetry run python ./get_power_kpi.py --start $DATA_INICIO --end $DATA_FIM --clusterid $DU_ID --roundtrip $ROUNDTRIP --cenario $CENARIO --identificador $IDENTIFICADOR
    echo ""
    echo ""
    echo "============================================================"
    echo "         Encerrando a GNB $DU_ID e $CONT RUs Emuladas       "
    echo "============================================================"
    echo "+ kill -9 ${pids[@]}"
	kill -9 "${pids[@]}"
	
    echo ""
    echo "============================================================"
    echo "Desmontando topologia da GNB/DU $DU_ID..."
    echo "============================================================"
    echo ""

    for ((j=1; j<=MAX_RUS; j++)); do
        NS="ru${j}"
        DU_IF="ofh_du${j}"

        if ip netns list | awk '{print $1}' | grep -qx "$NS"; then
            echo "+ ip netns del $NS"
            ip netns del "$NS"
        else
            echo "- namespace $NS já não existe"
        fi

        if ip link show "$DU_IF" >/dev/null 2>&1; then
            echo "+ ip link del $DU_IF"
            ip link del "$DU_IF"
        else
            echo "- interface $DU_IF já não existe"
        fi
    done

    echo ""
    echo "Topologia da GNB/DU $DU_ID removida."
    echo ""
    sleep 5s
done < "$ARQUIVO"

echo "Processamento finalizado."
