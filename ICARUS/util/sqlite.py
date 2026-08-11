import os
import sqlite3
from typing import Iterable, Mapping
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

BASE_DIR = os.path.abspath('../OUT')
print(f"BASE_DIR={BASE_DIR}")
Filename = os.environ["Filename"]
DB_PATH = os.path.join(BASE_DIR, f"{Filename}.db")
print(f"DB_PATH={DB_PATH}")

def _get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn

def init_db():
    with _get_connection() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            identificador TEXT NOT NULL,
            roundtrip INTEGER NOT NULL,
            cluster_id TEXT NOT NULL,
            cenario TEXT NOT NULL,
            timestamp_utc TEXT NOT NULL,
            metric TEXT NOT NULL,
            value REAL NOT NULL,
            unit TEXT,
            PRIMARY KEY (identificador, roundtrip, cluster_id, cenario, timestamp_utc, metric)
        )
        """)


def upsert_scenario(amostras: Iterable[Mapping]):
    sql = """
    INSERT INTO stats (
        identificador,
        roundtrip,
        cluster_id,
        cenario,
        timestamp_utc,
        metric,
        value,
        unit
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)

    ON CONFLICT (
        identificador,
        roundtrip,
        cluster_id,
        cenario,
        timestamp_utc,
        metric
    )
    DO UPDATE SET
        value = excluded.value,
        unit = excluded.unit
    """

    values = []

    for indice, amostra in enumerate(amostras):
        values.append((
            amostra["identificador"],
            amostra["roundtrip"],
            amostra["cluster_id"],
            amostra["cenario"],
            amostra["timestamp_utc"],
            amostra["metric"],
            amostra["value"],
            amostra["unit"]
        ))
    if not values:
        raise ValueError("Nenhuma amostra recebida para gravacao")
    conn = _get_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.executemany(sql, values)
        conn.commit()
    except Exception:
        conn.rollback()
        print("Erro ao inserir amostras no banco de dados. Desfazendo alterações.", flush=True)
        raise
    finally:
        conn.close()
