"""
Shim de compatibilidad para código legacy (app/cleaner/loader.py y app/db.py)
que importa `from app.config import DATA_DATABASE_URL`.

El proyecto moderno usa `app.core.config.load_settings()`. Este módulo solo
expone las URLs de BD como variables de módulo para no romper el código viejo.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Cargar .env desde la raíz si no se cargó antes
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# DATA_DATABASE_URL: lo usa app/cleaner/loader.py (psycopg2)
# Fallback a DATA_PLATFORM_DATABASE_URL por si solo tienes esa.
DATA_DATABASE_URL: str = (
    os.getenv("DATA_DATABASE_URL")
    or os.getenv("DATA_PLATFORM_DATABASE_URL")
    or ""
)