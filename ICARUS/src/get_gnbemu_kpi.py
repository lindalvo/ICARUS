import argparse
import json
import time
from ICARUS.util.functions import normalize_timestamp_utc
import websocket
from dotenv import find_dotenv, load_dotenv
from ICARUS.util.sqlite import init_db, upsert_scenario
load_dotenv(find_dotenv())

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
            print(f"Gravando Métrica OFH UL/DL Identificador {identificador} rodada {roundtrip} cluster {cluster_id} cenário {cenario} no banco de dados")
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
        # Métricas de Recursos de Hardware
        if "app_resource_usage" in metrica:
            recursos = metrica["app_resource_usage"]

            cpu = recursos.get("cpu_usage_percent")
            memoria = recursos.get("mem_total_mb")
            potencia = recursos.get("power_consumption_watts")

            if cpu is not None:
                upsert_scenario(
                    identificador,
                    roundtrip,
                    cluster_id,
                    cenario,
                    timestamp_utc=timestamp_utc,
                    metric="cpu_usage",
                    value=cpu,
                    unit="%",
                )
                print(f"Gravando Métrica CPU Identificador {identificador} rodada {roundtrip} cluster {cluster_id} cenário {cenario} no banco de dados")
                
            if memoria is not None:
                upsert_scenario(
                    identificador,
                    roundtrip,
                    cluster_id,
                    cenario,
                    timestamp_utc=timestamp_utc,
                    metric="memory_usage",
                    value=memoria,
                    unit="MB",
                )
                print(f"Gravando Métrica Memória Identificador {identificador} rodada {roundtrip} cluster {cluster_id} cenário {cenario} no banco de dados")

            if potencia is not None:
                upsert_scenario(
                    identificador,
                    roundtrip,
                    cluster_id,
                    cenario,
                    timestamp_utc=timestamp_utc,
                    metric="cpu_package_power",
                    value=potencia,
                    unit="W",
                )
                print(f"Gravando Métrica Consumo de Energia Identificador {identificador} rodada {roundtrip} cluster {cluster_id} cenário {cenario} no banco de dados")
        # Métricas do Scheduller
        if "cells" in metrica:
            max_latencies = []
            for celula in metrica["cells"]:
                max_latency = celula["cell_metrics"]["max_latency"]
                max_latencies.append(max_latency)
            print(f"Gravando Métrica Latência máxima do Sheduller Identificador {identificador} rodada {roundtrip} cluster {cluster_id} cenário {cenario} no banco de dados")
            if max_latencies:
                upsert_scenario(identificador, roundtrip, cluster_id, cenario,
                    timestamp_utc=timestamp_utc,
                    metric="max_scheduler_latency",
                    value=max(max_latencies),
                    unit="µs"
                )

    ws.close()
