import argparse
import json
import time
from ICARUS.util.functions import normalize_timestamp_utc
import websocket
import sys
from websocket import (
    WebSocketConnectionClosedException,
    WebSocketException,
)
from dotenv import find_dotenv, load_dotenv
from ICARUS.util.sqlite import init_db, upsert_scenario
load_dotenv(find_dotenv())

def emitir_estado(estado, **dados):
    detalhes = " ".join(f"{chave}={valor}" for chave, valor in dados.items())
    print(f"KPI_STATE={estado} {detalhes}".rstrip(), flush=True)

def adicionar_amostra(timestamp_utc, metric, value, unit):
    if value is None:
        return

    amostras.append({
        "identificador": identificador,
        "roundtrip": roundtrip,
        "cluster_id": cluster_id,
        "cenario": cenario,
        "timestamp_utc": timestamp_utc,
        "metric": metric,
        "value": value,
        "unit": unit,
    })

    if len(amostras) == 1:
        emitir_estado("FIRST_SAMPLE")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--seconds",
        required=True,
        type=int,
        help="Duração da janela em segundos",
    )

    parser.add_argument(
        "--roundtrip",
        required=True,
        type=int,
        help="Rodada de teste para identificar o arquivo de saída. Ex: 1, 2, 3, etc.",
    )

    parser.add_argument(
        "--clusterid",
        required=True,
        help="ID do cluster",
    )

    parser.add_argument(
        "--cenario",
        required=True,
        help="Cenário de teste para identificar o arquivo de saída. max link ou otimizado"
    )

    parser.add_argument(
        "--identificador",
        required=True,
        help="Identificador do Município Operadora"
    )


    args = parser.parse_args()
    identificador, roundtrip, cluster_id, cenario = args.identificador, args.roundtrip, args.clusterid, args.cenario
    init_db()
    ws = None
    amostras = []
    emitir_estado("CONNECTING")
    try:
        ws = websocket.create_connection(
            "ws://127.0.0.1:8001",
            timeout=10,
        )
    except (OSError, WebSocketException) as exc:
        emitir_estado("ERROR", code=10, type="connection_failed", message=repr(exc))
        sys.exit(10)

    try:
        ws.send(json.dumps({"cmd": "metrics_subscribe"}))
    except WebSocketException as exc:
        emitir_estado("ERROR", code=11, type="subscription_failed", message=repr(exc))
        ws.close()
        sys.exit(11)

    emitir_estado("SUBSCRIBED")
    
    inicio = time.monotonic()
    try:
        while time.monotonic() - inicio < args.seconds:
            mensagem = ws.recv()
            metrica = json.loads(mensagem)
            timestamp_utc = normalize_timestamp_utc(metrica["timestamp"])
            # Métricas do Open Fronthaul
            if "ru" in metrica:
                celulas_ofh = metrica["ru"]["ofh"]["cells"]
                throughput_ul_total = 0
                throughput_dl_total = 0
                for celula in celulas_ofh:
                    pci = celula["pci"]
                    throughput_ul = celula["ul"]["ethernet_receiver"]["average_throughput_mbps"]
                    throughput_dl = celula["dl"]["ethernet_transmitter"]["average_throughput_mbps"]
                    throughput_ul_total += throughput_ul
                    throughput_dl_total += throughput_dl
                #print(f"Gravando Métrica OFH UL/DL Identificador {identificador} rodada {roundtrip} cluster {cluster_id} cenário {cenario} no banco de dados")
                adicionar_amostra(timestamp_utc, "ofh_ul_throughput", throughput_ul_total, "Mbps")
                adicionar_amostra(timestamp_utc, "ofh_dl_throughput", throughput_dl_total, "Mbps")
            # Métricas de Recursos de Hardware
            if "app_resource_usage" in metrica:
                recursos = metrica["app_resource_usage"]

                cpu = recursos.get("cpu_usage_percent")
                memoria = recursos.get("mem_total_mb")
                potencia = recursos.get("power_consumption_watts")

                if cpu is not None:
                    adicionar_amostra(timestamp_utc, "cpu_usage", cpu, "%")
                if memoria is not None:
                    adicionar_amostra(timestamp_utc, "memory_usage", memoria, "MB")
                if potencia is not None:
                    adicionar_amostra(timestamp_utc, "cpu_package_power", potencia, "W")
            # Métricas do Scheduller
            if "cells" in metrica:
                max_latencies = []
                for celula in metrica["cells"]:
                    max_latency = celula["cell_metrics"]["max_latency"]
                    max_latencies.append(max_latency)
                #print(f"Gravando Métrica Latência máxima do Sheduller Identificador {identificador} rodada {roundtrip} cluster {cluster_id} cenário {cenario} no banco de dados")
                if max_latencies:
                    adicionar_amostra(timestamp_utc, "max_scheduler_latency", max(max_latencies), "µs")
    except WebSocketConnectionClosedException as exc:
        ws.close()
        if not amostras:
            emitir_estado("ERROR", code=12, type="connection_closed", message=repr(exc))
            sys.exit(12)
        emitir_estado("ERROR", code=13, type="websocket_closed_after_partial_collection", message=repr(exc), samples_collected=len(amostras), message=repr(exc))
        sys.exit(13)
    finally:
        if ws is not None:
            ws.close()
    upsert_scenario(amostras)