import os
import sqlite3
from pathlib import Path
from typing import Iterator
from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv())

DB_PATH = Path(os.environ["OUT_DIR"]).resolve() / "icarus.db"

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
            PRIMARY KEY (identificador, roundtrip, cluster_id, cenario)
        )
        """)


def upsert_scenario(identificador, roundtrip, cluster_id, cenario, **fields):
    if not fields:
        return

    base = {
        "identificador": identificador,
        "roundtrip": roundtrip,
        "cluster_id": cluster_id,
        "cenario": cenario,
        **fields
    }

    columns = list(base.keys())
    placeholders = ", ".join("?" for _ in columns)
    column_names = ", ".join(columns)

    update_columns = [c for c in fields.keys()]
    update_clause = ", ".join(f"{c} = excluded.{c}" for c in update_columns)

    sql = f"""
    INSERT INTO scenarios ({column_names})
    VALUES ({placeholders})
    ON CONFLICT(identificador, roundtrip, cluster_id, cenario)
    DO UPDATE SET {update_clause}
    """

    values = [base[c] for c in columns]

    with _get_connection() as conn:
        conn.execute(sql, values)
        conn.commit()

