from typing import Iterable, Tuple, List
from math import radians, sin, cos, atan2, sqrt
from datetime import datetime, timezone

EARTH_RADIUS_KM: float = 6371.0

def designacao_para_mhz(designacao: str) -> int:
    bw = designacao[:4].upper()

    if "M" in bw:
        partes = bw.split("M")
        mhz = float(f"{partes[0]}.{partes[1] or '0'}")
    elif "K" in bw:
        partes = bw.split("K")
        khz = float(f"{partes[0]}.{partes[1] or '0'}")
        mhz = khz / 1000
    elif "G" in bw:
        partes = bw.split("G")
        ghz = float(f"{partes[0]}.{partes[1] or '0'}")
        mhz = ghz * 1000
    else:
        mhz = float(bw)

    return int(mhz)

def haversine_distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    phi1, phi2 = radians(lat1), radians(lat2)
    dphi = radians(lat2 - lat1)
    dlambda = radians(lon2 - lon1)
    a = sin(dphi/2)**2 + cos(phi1)*cos(phi2)*sin(dlambda/2)**2
    c = 2 * atan2(sqrt(a), sqrt(1-a))
    return EARTH_RADIUS_KM * c

def normalize_timestamp_utc(timestamp):
    if isinstance(timestamp, datetime):
        dt = timestamp
    else:
        timestamp = str(timestamp)

        if timestamp.endswith("Z"):
            timestamp = timestamp[:-1] + "+00:00"

        dt = datetime.fromisoformat(timestamp)

    if dt.tzinfo is None:
        # Somente está correto se o timestamp sem timezone já representar UTC.
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)

    return dt.isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")