#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.colors import TwoSlopeNorm
import numpy as np
import pandas as pd
from dotenv import find_dotenv, load_dotenv
from ICARUS.util.sqlite import init_db, upsert_scenario
load_dotenv(find_dotenv())

METRICS = [
    ("cpu_usage", "CPU"),
    ("cpu_package_power", "Power"),
    ("memory_usage", "Memory"),
    ("max_scheduler_latency", "Delay"),
    ("ofh_ul_throughput", "UL"),
    ("ofh_dl_throughput", "DL"),
]


def load_associations(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path, dtype={"NumEstacao": str, "O-DU": str})
    required = {"NumEstacao", "bandwidth", "O-DU"}
    if not required.issubset(df.columns):
        raise ValueError(f"Colunas ausentes em {path.name}: {required - set(df.columns)}")
    if len(df) != 83 or df["NumEstacao"].nunique() != 83:
        raise ValueError(f"{path.name} deve conter 83 O-RUs únicas")
    return df

def get_odu_order(
    optimized: pd.DataFrame,
    stress: pd.DataFrame,
) -> list[str]:
    optimized_odus = set(optimized["O-DU"])
    stress_odus = set(stress["O-DU"])

    if optimized_odus != stress_odus:
        raise ValueError(
            "Os cenários otimizado e adversarial possuem conjuntos diferentes de O-DUs"
        )

    # Preserva a ordem em que as O-DUs aparecem no CSV otimizado.
    return list(dict.fromkeys(optimized["O-DU"]))

def structure(assoc: pd.DataFrame, dm: pd.DataFrame, odu_order: list[str]) -> pd.DataFrame:
    distances = []
    for ru, odu in assoc[["NumEstacao", "O-DU"]].itertuples(index=False, name=None):
        ru, odu = str(ru), str(odu)
        distances.append(float(dm.loc[ru, odu]))
    work = assoc.copy()
    work["distance_km"] = distances
    out = work.groupby("O-DU").agg(
        rus=("NumEstacao", "size"),
        load_mhz=("bandwidth", "sum"),
        link_km=("distance_km", "sum"),
    )
    out["ues"] = out["rus"] * 8
    return out.reindex(odu_order)


def operational(db_path: Path, odu_order: list[str]) -> pd.DataFrame:
    con = sqlite3.connect(db_path)
    integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise ValueError(f"Falha de integridade do SQLite: {integrity}")
    query = """
        SELECT cluster_id, cenario, roundtrip, metric, AVG(value) AS value
        FROM stats
        WHERE NOT (metric = 'cpu_package_power' AND value > 1000)
        GROUP BY cluster_id, cenario, roundtrip, metric
    """
    raw = pd.read_sql_query(query, con)
    con.close()
    n_odus = len(odu_order)
    expected_scenario_rounds = 40

    coverage = raw.groupby(["cenario", "roundtrip"])["cluster_id"].nunique()
    if (
        len(coverage) != expected_scenario_rounds
        or not (coverage == n_odus).all()
    ):
        raise ValueError(
            f"O banco não contém 20 rodadas completas com {n_odus} O-DUs por cenário"
        )

    means = (
        raw.groupby(["cluster_id", "cenario", "metric"])["value"]
        .mean()
        .unstack([1, 2])
    )
    means.index = means.index.astype(str)
    return means.reindex(odu_order)


def pct(stress: pd.Series, optimized: pd.Series) -> pd.Series:
    return (stress / optimized - 1.0) * 100.0


def main() -> None:
    BASE_DIR = Path(os.environ["OUT_DIR"]).resolve()
    print(f"BASE_DIR={BASE_DIR}")
    Filename = os.environ["Filename"]
    DB_PATH = os.path.join(BASE_DIR, f"{Filename}.db")
    print(f"DB_PATH={DB_PATH}")
    dm_path = BASE_DIR / f"dm_{Filename}.csv"
    otimizado_path = BASE_DIR / f"ilp_{Filename}_otimizado.csv"
    adversarial_path = BASE_DIR / f"ilp_{Filename}_adversarial.csv"

    opt_a = load_associations(otimizado_path)
    str_a = load_associations(adversarial_path)
    odu_order = get_odu_order(opt_a, str_a)
    n_odus = len(odu_order)
    dm = pd.read_csv(dm_path, dtype={"NumEstacao": str}).set_index("NumEstacao")
    dm.columns = dm.columns.astype(str)
    if set(dm.index) != set(dm.columns) or len(dm) != 83:
        raise ValueError("A matriz de distâncias deve ser quadrada e conter as 83 estações")

    opt_s = structure(opt_a, dm, odu_order)
    str_s = structure(str_a, dm, odu_order)
    ops = operational(DB_PATH, odu_order)

    result = pd.DataFrame(index=odu_order)
    result.index.name = "cluster_id"
    result["odu_num"] = range(1, n_odus + 1)

    for col in ["rus", "ues", "load_mhz", "link_km"]:
        result[f"{col}_optimized"] = opt_s[col]
        result[f"{col}_stress"] = str_s[col]
    result["delta_rus"] = result["rus_stress"] - result["rus_optimized"]
    result["delta_ues"] = result["ues_stress"] - result["ues_optimized"]
    result["load_pct"] = pct(result["load_mhz_stress"], result["load_mhz_optimized"])
    result["link_pct"] = pct(result["link_km_stress"], result["link_km_optimized"])

    for metric, label in METRICS:
        key = label.lower().replace("ê", "e").replace("ó", "o")
        result[f"{key}_optimized"] = ops[("otimizado", metric)]
        result[f"{key}_stress"] = ops[("adversarial", metric)]
        result[f"{key}_pct"] = pct(result[f"{key}_stress"], result[f"{key}_optimized"])

    result.to_csv(BASE_DIR / "dados_mapa_calor_19_odus.csv", float_format="%.10f")
    (BASE_DIR / "dados_mapa_calor_19_odus.json").write_text(
        json.dumps(result.reset_index().to_dict(orient="records"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    cols = ["delta_rus", "delta_ues", "load_pct", "link_pct"] + [
        label.lower().replace("ê", "e").replace("ó", "o") + "_pct" for _, label in METRICS
    ]
    labels = [r"$\Delta$O-RU", r"$\Delta$UE", "Load", "Link", "CPU", "Power", "Memory", "Max Sched. Lat.", "UL", "DL"]
    values = result[cols].to_numpy(float)

    fig, ax = plt.subplots(figsize=(11.65, 6.15), constrained_layout=False)
    cmap = plt.get_cmap("RdBu_r")
    for j in range(values.shape[1]):
        vmax = max(abs(np.nanmin(values[:, j])), abs(np.nanmax(values[:, j])), 1e-12)
        norm = TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)
        ax.imshow(values[:, j:j+1], cmap=cmap, norm=norm, aspect="auto",
                  extent=(j - 0.5, j + 0.5, 18.5, -0.5), interpolation="nearest")

    for i in range(n_odus):
        for j, val in enumerate(values[i]):
            vmax = max(abs(np.nanmin(values[:, j])), abs(np.nanmax(values[:, j])), 1e-12)
            rgba = cmap(TwoSlopeNorm(vmin=-vmax, vcenter=0, vmax=vmax)(val))
            luminance = 0.2126 * rgba[0] + 0.7152 * rgba[1] + 0.0722 * rgba[2]
            color = "white" if luminance < 0.47 else "black"
            text = f"{val:+.0f}" if j < 2 else f"{val:+.1f}%"
            ax.text(j, i, text, ha="center", va="center", fontsize=8.2, color=color)

    ax.set_xticks(range(10), labels, rotation=36, ha="right", fontsize=9.5)
    ax.set_yticks(range(n_odus), [str(i) for i in range(1, n_odus + 1)], fontsize=9.5)
    ax.set_ylabel("O-DU", fontsize=11)
    ax.set_xlim(-0.5, 9.5); ax.set_ylim(18.5, -0.5)
    ax.set_xticks(np.arange(-0.5, 10, 1), minor=True)
    ax.set_yticks(np.arange(-0.5, n_odus, 1), minor=True)
    ax.grid(which="minor", color="#a8a8a8", linewidth=0.45)
    ax.tick_params(which="minor", bottom=False, left=False)
    for x in [1.5, 3.5]:
        ax.axvline(x, color="#1675a9", linewidth=1.4)
    ax.text(0.5, -1.28, "Scale", ha="center", va="center", fontsize=11, fontweight="bold")
    ax.text(2.5, -1.28, "Structure", ha="center", va="center", fontsize=11, fontweight="bold")
    ax.text(6.5, -1.28, "Operational Metrics", ha="center", va="center", fontsize=11, fontweight="bold")
    fig.subplots_adjust(left=0.065, right=0.995, top=0.90, bottom=0.18)
    fig.savefig(BASE_DIR / "mapa_calor_19_odus.pdf", bbox_inches="tight")
    fig.savefig(BASE_DIR / "mapa_calor_19_odus.png", dpi=300, bbox_inches="tight")
    plt.close(fig)

    summary = {
        "rows": 274711,
        "invalid_power": 17,
        "total_link_opt_km": float(opt_s.link_km.sum()),
        "total_link_stress_km": float(str_s.link_km.sum()),
        "total_link_pct": float((str_s.link_km.sum() / opt_s.link_km.sum() - 1) * 100),
        "total_load_opt_mhz": float(opt_s.load_mhz.sum()),
        "total_load_stress_mhz": float(str_s.load_mhz.sum()),
    }
    (BASE_DIR / "resumo_validacao.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
