#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${SCRIPT_DIR}/../../.env"
set -a             
source "$ENV_FILE"
set +a

FULL_PATH=$(realpath ../.$OUT_DIR)

ROUNDTRIPS=5

# armazenando rodadas anteriores
#
DATE=$(date +"%Y%m%d%H%M")
echo "+ mv $FULL_PATH/icarus.db" "$FULL_PATH/icarus$DATE.db"
mv "${FULL_PATH}/icarus.db" "${FULL_PATH}/icarus${DATE}.db" 2>/dev/null || true

echo "Iniciando processamento dos arquivos em: $FULL_PATH"
echo "--------------------------------------------------"

# ==========================================
# LOOP PRINCIPAL
# ==========================================
# Iterar sobre todos os arquivos .txt da pasta que começam com 'pipeline_'
for arquivo_caminho in "$FULL_PATH"/pipeline_*.txt; do
    
    # Se não houver nenhum arquivo que case com o padrão inicial, o bash pode retornar o próprio padrão literal.
    # Esta linha garante que o arquivo realmente existe antes de prosseguir.
    [ -e "$arquivo_caminho" ] || continue

    # Extrai apenas o nome do arquivo (remove o caminho da pasta)
    arquivo_nome=$(basename "$arquivo_caminho")

    # Expressão Regular para validar o formato exato:
    # ^pipeline_             -> Começa com pipeline_
    # [0-9]{11}_[0-9]{7}_    -> 11 dígitos, underline, 7 dígitos, underline
    # (max_load|total_distance) -> Uma das duas opções de texto
    # \.txt$                 -> Termina obrigatoriamente com .txt
    regex="^pipeline_([0-9]{11}_[0-9]{7})_(max_load|total_distance)\.txt$"

    if [[ "$arquivo_nome" =~ $regex ]]; then
        # Captura os grupos da expressão regular
        # ${BASH_REMATCH[1]} captura o bloco do identificador (11 dígitos + _ + 7 dígitos)
        # ${BASH_REMATCH[2]} captura o tipo de cálculo (max_load ou total_distance)
        IDENTIFICADOR="${BASH_REMATCH[1]}"
        TIPO_CALCULO="${BASH_REMATCH[2]}"

        echo ""
        echo "=================================================="
        echo "Arquivo encontrado: $arquivo_nome"
        echo "ID Extraído: $IDENTIFICADOR"
        echo "Tipo Extraído: $TIPO_CALCULO"
        echo "=================================================="

        # Loop interno: Executa o script filho X vezes (de 1 até o LIMITE_ROUNDTRIP)
        for (( i=1; i<=ROUNDTRIPS; i++ )); do
            echo "-> Executando rodada $i de $ROUNDTRIPS..."
            
            echo "+ ./run_topology.sh $arquivo_caminho $i $TIPO_CALCULO $IDENTIFICADOR"
            ./run_topology.sh "$arquivo_caminho" "$i" "$TIPO_CALCULO" "$IDENTIFICADOR"
            if [ $? -ne 0 ]; then
                echo "Aviso: O script filho retornou um erro na rodada $i para o arquivo $arquivo_nome"
            fi
        done

    else
        echo "Ignorado (Formato inválido): $arquivo_nome"
    fi
done

echo "--------------------------------------------------"
echo "Processamento concluído!"
