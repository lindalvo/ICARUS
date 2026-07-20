#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../.env"

set -a
source "$ENV_FILE"
set +a

FULL_PATH=$(realpath ../."$OUT_DIR")

ROUNDTRIPS=30

# ==========================================
# ARMAZENAMENTO DAS RODADAS ANTERIORES
# ==========================================
DATE=$(date +"%Y%m%d%H%M")


echo "Iniciando processamento dos arquivos em: $FULL_PATH"
echo "--------------------------------------------------"

# ==========================================
# ARRAYS DOS ARQUIVOS VÁLIDOS
# ==========================================
arquivos_caminho=()
arquivos_nome=()
identificadores=()
tipos_calculo=()

regex="^pipeline_([0-9]{11}_[0-9]{7})_([1-9])_(cpu_power|opex_capex)\.txt$"

# ==========================================
# LOCALIZAÇÃO E VALIDAÇÃO DOS ARQUIVOS
# ==========================================
for arquivo_caminho in "$FULL_PATH"/pipeline_*.txt; do

    [ -e "$arquivo_caminho" ] || continue

    arquivo_nome=$(basename "$arquivo_caminho")

    if [[ "$arquivo_nome" =~ $regex ]]; then
        IDENTIFICADOR="${BASH_REMATCH[1]}"
        TIPO_CALCULO="${BASH_REMATCH[2]}"

        arquivos_caminho+=("$arquivo_caminho")
        arquivos_nome+=("$arquivo_nome")
        identificadores+=("$IDENTIFICADOR")
        tipos_calculo+=("$TIPO_CALCULO")

        echo "Arquivo válido encontrado: $arquivo_nome"
        echo "  Identificador: $IDENTIFICADOR"
        echo "  Tipo cálculo:  $TIPO_CALCULO"
    else
        echo "Ignorado (formato inválido): $arquivo_nome"
    fi
done

TOTAL_ARQUIVOS=${#arquivos_caminho[@]}

if (( TOTAL_ARQUIVOS == 0 )); then
    echo "Nenhum arquivo válido foi encontrado em: $FULL_PATH"
    exit 0
else 
    echo "Total de arquivos válidos encontrados: $TOTAL_ARQUIVOS"
    echo "Armazenando o banco de dados atual como icarus${DATE}.db"
    echo "+ mv ${FULL_PATH}/icarus.db ${FULL_PATH}/icarus${DATE}.db"
    mv "${FULL_PATH}/icarus.db" "${FULL_PATH}/icarus${DATE}.db" 2>/dev/null || true
fi

# ==========================================
# LOOP PRINCIPAL
# ==========================================
for (( i=1; i<=ROUNDTRIPS; i++ )); do

    echo ""
    echo "##################################################"
    echo "INICIANDO ROUNDTRIP $i DE $ROUNDTRIPS"
    echo "##################################################"

    # Percorre todos os arquivos válidos.
    for (( indice=0; indice<TOTAL_ARQUIVOS; indice++ )); do

        arquivo_caminho="${arquivos_caminho[$indice]}"
        arquivo_nome="${arquivos_nome[$indice]}"
        IDENTIFICADOR="${identificadores[$indice]}"
        TIPO_CALCULO="${tipos_calculo[$indice]}"

        echo ""
        echo "=================================================="
        echo "Roundtrip:      $i de $ROUNDTRIPS"
        echo "Arquivo:        $arquivo_nome"
        echo "Identificador:  $IDENTIFICADOR"
        echo "Tipo de cálculo: $TIPO_CALCULO"
        echo "=================================================="
        echo "+ ./run_topology.sh $arquivo_caminho $i $TIPO_CALCULO $IDENTIFICADOR"
        ./run_topology.sh "$arquivo_caminho" "$i" "$TIPO_CALCULO" "$IDENTIFICADOR"

        status=$?

        if (( status != 0 )); then
            echo "Aviso: run_topology.sh retornou erro."
            echo "  Roundtrip: $i"
            echo "  Arquivo:   $arquivo_nome"
            echo "  Status:    $status"
        fi
    done

    echo ""
    echo "ROUNDTRIP $i CONCLUÍDO"
done

echo ""
echo "--------------------------------------------------"
echo "Processamento concluído!"