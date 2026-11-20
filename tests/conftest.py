import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DISABLE_SECRET_MANAGER", "1")
os.environ.setdefault("STORE_BACKEND", "json")
