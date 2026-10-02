"""
Inspecciona el schema `data` para las tablas que necesita el seed.
Uso:
    python scripts/check_schema.py
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import asyncpg
from app.core.config import load_settings


TABLES = ("geographic_levels", "methodologies", "sources", "dimensions", "indicators")


async def main() -> None:
    settings = load_settings("schema-check")
    pool = await asyncpg.create_pool(settings.db_dsn, min_size=1, max_size=2)

    # 1. Columnas de cada tabla
    print("=" * 80)
    print("COLUMNAS")
    print("=" * 80)
    rows = await pool.fetch(
        """
        SELECT table_name, column_name, data_type, is_nullable,
               column_default, character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = 'data'
          AND table_name = ANY($1::text[])
        ORDER BY table_name, ordinal_position
        """,
        list(TABLES),
    )
    current = None
    for r in rows:
        if r["table_name"] != current:
            current = r["table_name"]
            print(f"\n--- data.{current} ---")
        nullable = "" if r["is_nullable"] == "NO" else " (nullable)"
        length = f"({r['character_maximum_length']})" if r["character_maximum_length"] else ""
        default = f" DEFAULT {r['column_default']}" if r["column_default"] else ""
        print(f"  {r['column_name']:30s} {r['data_type']}{length}{nullable}{default}")

    # 2. Constraints (NOT NULL, CHECK, UNIQUE, PK, FK)
    print()
    print("=" * 80)
    print("CONSTRAINTS")
    print("=" * 80)
    rows = await pool.fetch(
        """
        SELECT
            tc.table_name,
            tc.constraint_type,
            tc.constraint_name,
            kcu.column_name,
            pg_get_constraintdef(pc.oid) AS constraint_def
        FROM information_schema.table_constraints tc
        JOIN information_schema.key_column_usage kcu
          ON kcu.constraint_name = tc.constraint_name
         AND kcu.table_schema = tc.table_schema
        JOIN pg_constraint pc ON pc.conname = tc.constraint_name
        WHERE tc.table_schema = 'data'
          AND tc.table_name = ANY($1::text[])
        ORDER BY tc.table_name, tc.constraint_type, kcu.ordinal_position
        """,
        list(TABLES),
    )
    current = None
    for r in rows:
        if r["table_name"] != current:
            current = r["table_name"]
            print(f"\n--- data.{current} ---")
        print(f"  [{r['constraint_type']:11s}] {r['constraint_name']}")
        if r["constraint_def"] and r["constraint_type"] in ("CHECK", "UNIQUE", "PRIMARY KEY"):
            print(f"                {r['constraint_def']}")

    # 3. Estado actual de las tablas de referencia
    print()
    print("=" * 80)
    print("CONTENIDO ACTUAL")
    print("=" * 80)
    for t in TABLES:
        count = await pool.fetchval(f"SELECT COUNT(*) FROM data.{t}")
        print(f"  data.{t:25s} -> {count} filas")

    # 4. Detalle de lo que hay en methodologies, sources, geographic_levels
    print()
    print("=" * 80)
    print("METODOLOGÍAS")
    print("=" * 80)
    for r in await pool.fetch("SELECT * FROM data.methodologies"):
        print(" ", dict(r))

    print()
    print("=" * 80)
    print("SOURCES")
    print("=" * 80)
    for r in await pool.fetch("SELECT * FROM data.sources"):
        print(" ", dict(r))

    print()
    print("=" * 80)
    print("GEOGRAPHIC LEVELS")
    print("=" * 80)
    for r in await pool.fetch("SELECT * FROM data.geographic_levels"):
        print(" ", dict(r))

    print()
    print("=" * 80)
    print("DIMENSIONS")
    print("=" * 80)
    for r in await pool.fetch("SELECT * FROM data.dimensions"):
        print(" ", dict(r))

    await pool.close()


if __name__ == "__main__":
    asyncio.run(main())