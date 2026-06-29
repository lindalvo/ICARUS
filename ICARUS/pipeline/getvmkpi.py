#!/usr/bin/env python3

import argparse
import atexit
import ssl
from datetime import datetime
import os
from dotenv import find_dotenv, load_dotenv
import pandas as pd
from pyVim.connect import SmartConnect, Disconnect
from pyVmomi import vim

load_dotenv(find_dotenv())

env_var_names = list(os.environ)

print(env_var_names)

VCENTER_IP = os.environ["VCENTER_IP"]
VCENTER_USER = os.environ["VCENTER_USER"]
VCENTER_PASSWORD = os.environ["VCENTER_PASSWORD"]
VM_NAME = os.environ["VCENTER_VM"]

# Intervalo realtime típico do vSphere.
# Use 20 se suas amostras no govc aparecem de 20 em 20 segundos.
INTERVAL_ID = 20

# Corrige o problema comum no ESXi/vCenter 7.x em que power de VM vem multiplicado por 1000.
# Pelos seus dados, 28268 provavelmente significa 28.268 W.
FIX_ESXI7_VM_POWER_SCALE = True

METRICS = [
    "power.power.average",
    "power.energy.summation",
    "cpu.usagemhz.average",
    "cpu.ready.summation",
    "cpu.costop.summation",
    "mem.active.average",
    "mem.consumed.average",
]


def collect_vcenter_vm_metrics(start: str, end: str) -> pd.DataFrame:
    """
    Coleta métricas da VM gercom8 no vCenter entre start e end.

    start/end no formato:
        2026-06-27T13:01:00.000Z

    Retorna:
        pandas.DataFrame em formato longo:
        timestamp_utc, interval_sec, metric, value, unit
    """

    ssl_context = ssl._create_unverified_context()

    service_instance = SmartConnect(
        host=VCENTER_IP,
        user=VCENTER_USER,
        pwd=VCENTER_PASSWORD,
        sslContext=ssl_context,
    )

    atexit.register(Disconnect, service_instance)

    content = service_instance.RetrieveContent()
    perf_manager = content.perfManager

    # Converte as strings do bash para datetime UTC.
    # Mantém datetime "naive" em UTC, compatível com o padrão usado pelo pyVmomi.
    start_time = datetime.strptime(start, "%Y-%m-%dT%H:%M:%S.000Z")
    end_time = datetime.strptime(end, "%Y-%m-%dT%H:%M:%S.000Z")

    # Localiza a VM pelo nome.
    vm_view = content.viewManager.CreateContainerView(
        content.rootFolder,
        [vim.VirtualMachine],
        True,
    )

    vm = None
    for obj in vm_view.view:
        if obj.name == VM_NAME:
            vm = obj
            break

    vm_view.Destroy()

    # Monta o mapa nome_da_metrica -> counter_id.
    counter_id_by_name = {}
    counter_name_by_id = {}
    counter_unit_by_id = {}

    for counter in perf_manager.perfCounter:
        metric_name = (
            f"{counter.groupInfo.key}."
            f"{counter.nameInfo.key}."
            f"{counter.rollupType}"
        )

        counter_id_by_name[metric_name] = counter.key
        counter_name_by_id[counter.key] = metric_name
        counter_unit_by_id[counter.key] = counter.unitInfo.label

    # Monta a lista de métricas a consultar.
    metric_ids = []

    for metric_name in METRICS:
        metric_ids.append(
            vim.PerformanceManager.MetricId(
                counterId=counter_id_by_name[metric_name],
                instance="",
            )
        )

    # Consulta as métricas no intervalo start/end.
    query_spec = vim.PerformanceManager.QuerySpec(
        entity=vm,
        metricId=metric_ids,
        startTime=start_time,
        endTime=end_time,
        intervalId=INTERVAL_ID,
        format="normal",
    )

    perf_results = perf_manager.QueryPerf(querySpec=[query_spec])

    rows = []

    for result in perf_results:
        for series in result.value:
            metric_name = counter_name_by_id[series.id.counterId]
            unit = counter_unit_by_id[series.id.counterId]

            for sample_info, value in zip(result.sampleInfo, series.value):
                adjusted_value = value
                adjusted_unit = unit

                if FIX_ESXI7_VM_POWER_SCALE:
                    if metric_name == "power.power.average":
                        adjusted_value = value / 1000
                        adjusted_unit = "W"

                    if metric_name == "power.energy.summation":
                        adjusted_value = value / 1000
                        adjusted_unit = "J"

                rows.append(
                    {
                        "timestamp_utc": sample_info.timestamp,
                        "interval_sec": sample_info.interval,
                        "metric": metric_name,
                        "value": adjusted_value,
                        "unit": adjusted_unit,
                    }
                )

    df = pd.DataFrame(rows)

    df["timestamp_utc"] = pd.to_datetime(df["timestamp_utc"], utc=True)

    df = df.sort_values(
        by=["timestamp_utc", "metric"],
        ignore_index=True,
    )

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--start",
        required=True,
        help="Início da janela no formato 2026-06-27T13:01:00.000Z",
    )

    parser.add_argument(
        "--end",
        required=True,
        help="Fim da janela no formato 2026-06-27T13:06:00.000Z",
    )

    parser.add_argument(
        "--roudtrip",
        required=True,
        help="Rodada de teste para identificar o arquivo de saída. Ex: 1, 2, 3, etc.",
    )

    parser.add_argument(
        "--gnbid",
        required=True,
        help="ID do gNB",
    )

    args = parser.parse_args()

    df_metrics = collect_vcenter_vm_metrics(
        #start=args.start,
        #end=args.end,
        start="2026-06-27T13:01:00.000Z",
        end="2026-06-27T14:01:00.000Z"
    )

    print(df_metrics)

    df_metrics.to_csv(
        f"vcenter_vm_metrics_raw_{args.roudtrip}_{args.gnbid}.csv",
        index=False,
        encoding="utf-8",
    )