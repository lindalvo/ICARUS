from pathlib import Path

# Raiz do projeto (…/src/o_du_place/util/constants.py -> sobe 3 níveis -> raiz do repo)
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Subdiretórios de dados
ANATEL_DATA_DIR = PROJECT_ROOT / "ANATEL"
# Constantes físicas / de modelo
MAX_FIBER_DISTANCE_KM: float = 9.0
MAX_LOAD: int = 200  # Soma máxima de Frequências das bandas em MHz das RUs associadas a mesma DU
FIBER_DELAY_US_PER_KM: float = 5.0
