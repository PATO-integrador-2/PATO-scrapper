"""Corrige los nombres de los indicadores CEPALSTAT en la BD.

Uso:
    python scripts/fix_cepal_indicator_names.py
"""

from __future__ import annotations
import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import httpx
import asyncpg
from app.core.config import load_settings


async def fetch_indicator_metadata(indicator_id: int) -> dict:
    """Consulta metadata del indicador en CEPALSTAT."""
    base = "https://api-cepalstat.cepal.org/cepalstat/api/v1"
    url = f"{base}/indicator/{indicator_id}/metadata"
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
        r = await c.get(url, params={"lang": "es", "format": "json"})
        r.raise_for_status()
        payload = r.json()
    meta = (payload.get("body") or {}).get("metadata") or {}
    return {
        "name": meta.get("indicator_name"),
        "unit": meta.get("unit"),
        "theme": meta.get("theme"),
        "area": meta.get("area"),
        "definition": meta.get("definition"),
    }


async def main() -> None:
    settings = load_settings("fix-names")
    pool = await asyncpg.create_pool(settings.db_dsn, min_size=1, max_size=2)

    async with pool.acquire() as conn:
        # Corregir TODOS los que tengan name genérico (código o "CEPALSTAT...")
        rows = await conn.fetch(
            """
            SELECT i.id, i.code, i.name, i.unit
            FROM data.indicators i
            JOIN data.methodologies m ON m.id = i.methodology_id
            WHERE m.code = 'CEPAL_IPM'
              AND (i.name = i.code OR i.name LIKE 'CEPALSTAT%' OR i.name LIKE 'CEPAL%')
            """
        )
        print(f"Indicadores a corregir: {len(rows)}")

        for r in rows:
            code = r["code"]
            try:
                cepal_id = int(code.replace("CEPALSTAT_", ""))
            except ValueError:
                print(f"  ⚠ {code}: no se puede extraer ID")
                continue

            try:
                meta = await fetch_indicator_metadata(cepal_id)
            except Exception as exc:
                print(f"  ⚠ {code}: error consultando API -> {exc}")
                continue

            new_name = meta["name"] or code
            new_unit = meta["unit"] or r["unit"] or "Porcentaje"

            await conn.execute(
                """UPDATE data.indicators
                   SET name=$1,
                       unit=$2,
                       description=COALESCE($3, description)
                   WHERE id=$4""",
                new_name,
                new_unit,
                meta.get("definition"),
                r["id"],
            )
            print(f"  {code}:")
            print(f"    {r['name']!r} -> {new_name!r}")
            print(f"    unit: {new_unit!r}")

        # Corregir el dataset también (usaba el nombre genérico)
        await conn.execute(
            """
            UPDATE data.datasets d
            SET name = i.name
            FROM data.indicators i
            WHERE d.methodology_id = i.methodology_id
              AND d.code LIKE 'cepalstat_indicator_%'
              AND i.code = 'CEPALSTAT_' || REPLACE(d.code, 'cepalstat_indicator_', '')
            """
        )

        # Verificar datasets
        datasets = await conn.fetch(
            """
            SELECT d.code, d.name
            FROM data.datasets d
            JOIN data.methodologies m ON m.id = d.methodology_id
            WHERE m.code = 'CEPAL_IPM'
            """
        )
        print("\nDatasets CEPAL_IPM:")
        for d in datasets:
            print(f"  {d['code']}: {d['name']!r}")

    await pool.close()
    print("\nListo.")


if __name__ == "__main__":
    asyncio.run(main())