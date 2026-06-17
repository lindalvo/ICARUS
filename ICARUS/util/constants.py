from pathlib import Path

# Raiz do projeto (…/src/o_du_place/util/constants.py -> sobe 3 níveis -> raiz do repo)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Diretórios padrão
OUT_DIR = PROJECT_ROOT / "OUT"
MAPS_DIR = PROJECT_ROOT / "MAPS"

# Subdiretórios de dados
ANATEL_DATA_DIR = PROJECT_ROOT / "ANATEL"
Filename = "50418418705_3170206"
# Constantes físicas / de modelo
MAX_FIBER_DISTANCE_KM: float = 9.0
MAX_LOAD: int = 250  # Soma máxima de Frequências das bandas em MHz das RUs associadas a mesma DU
MAX_CLUSTER_SIZE: int = 5
FIBER_DELAY_US_PER_KM: float = 5.0