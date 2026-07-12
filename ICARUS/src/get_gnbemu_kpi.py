import argparse
import json
import time
import websocket
from dotenv import find_dotenv, load_dotenv
from ICARUS.util.sqlite import init_db, upsert_scenario
load_dotenv(find_dotenv())

if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--seconds",
        required=True,
        help="Duração da janela em segundos",
    )

    parser.add_argument(
        "--roundtrip",
        required=True,
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
        help="Cenário de teste para identificar o arquivo de saída. max link ou total_distance"
    )

    parser.add_argument(
        "--identificador",
        required=True,
        help="Identificador do Município Operadora"
    )


    args = parser.parse_args()
    identificador, roundtrip, cluster_id, cenario = args.identificador, args.roundtrip, args.clusterid, args.cenario
    init_db()
    ws = websocket.create_connection("ws://127.0.0.1:8001")
    ws.send(json.dumps({"cmd": "metrics_subscribe"}))
    inicio = time.monotonic()
    while time.monotonic() - inicio < args.seconds:
        mensagem = ws.recv()
        metrica = json.loads(mensagem)
        timestamp = metrica["timestamp"]
        timestamp_utc = f"{timestamp}Z"
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
            print(f"Gravando Métrica OFH UL/DL Identificado {identificador} rodada {roundtrip} cluster {cluster_id} cenário {cenario} no banco de dados")
            upsert_scenario(identificador, roundtrip, cluster_id, cenario,
                timestamp_utc=timestamp_utc,
                metric="ofh_ul_throughput",
                value=throughput_ul_total,
                unit="Mbps"
            )
            upsert_scenario(identificador, roundtrip, cluster_id, cenario,
                timestamp_utc=timestamp_utc,
                metric="ofh_dl_throughput",
                value=throughput_dl_total,
                unit="Mbps"
            )

        if "app_resource_usage" in metrica:
            cpu = metrica["app_resource_usage"]["cpu_usage_percent"]
            memoria = metrica["app_resource_usage"]["memory_usage_mb"]

            upsert_scenario(identificador, roundtrip, cluster_id, cenario,
                timestamp_utc=timestamp_utc,
                metric="cpu_usage",
                value=cpu,
                unit="%"
            )
            upsert_scenario(identificador, roundtrip, cluster_id, cenario,
                timestamp_utc=timestamp_utc,
                metric="memory_usage",
                value=memoria,
                unit="MB"
            )

        if "cells" in metrica:
            max_latencies = []
            for celula in metrica["cells"]:
                pci = celula["ue_list"][0]["pci"]
                max_latency = celula["cell_metrics"]["max_latency"]
                max_latencies.append(max_latency)
            upsert_scenario(identificador, roundtrip, cluster_id, cenario,
                timestamp_utc=timestamp_utc,
                metric="max_scheduler_latency",
                value=max(max_latencies),
                unit="µs"
            )


    ws.close()
