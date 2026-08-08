"""UI configuration values and filesystem paths."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DATA_DIRECTORY = PROJECT_ROOT / "data"

configured_path = os.environ.get("SIM_PLATFORM_DB_PATH")
if configured_path:
    candidate = Path(configured_path).expanduser()
    DATABASE_PATH = (
        candidate.resolve()
        if candidate.is_absolute()
        else (PROJECT_ROOT / candidate).resolve()
    )
else:
    DATABASE_PATH = DATA_DIRECTORY / "simulation_platform.sqlite3"

APP_VERSION = "0.1.0"
