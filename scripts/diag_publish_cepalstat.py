"""Verifica que existan todos los metadatos y geografías para el run CEPALSTAT."""

from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import asyncpg
from app.core.config import load_settings


async def main(run_id: str, indicator_id: int) -> None:
    settings = load_settings("diag")
    pool = await asyncpg.create_pool(settings.db_dsn, min_size=1, max_size=2)

    # 1. Obtener 1 registro de staging para ver exactamente las claves
    row = await pool.fetchrow(
        """
        SELECT sr.normalized_data
        FROM ingestion.staging_records sr
        JOIN ingestion.staging_resources sres ON sres.id = sr.staging_resource_id
        JOIN ingestion.etl_runs er ON er.id = sres.etl_run_id
        WHERE er.ingestion_run_id = $1 AND sr.quality_status = 'valid'
        LIMIT 1
        """,
        UUID(run_id),
    )
    d = row["normalized_data"]
    if isinstance(d, str):
        d = json.loads(d)

    print("=" * 70)
    print("Muestra de normalized_data")
    print("=" * 70)
    for k in ("methodology_code", "dataset_code", "dataset_version",
              "indicator_code", "geography_code", "year", "period", "value", "unit"):
        print(f"  {k:20s} = {d.get(k)!r}")

    # 2. ¿Existe la metodología?
    method_code = d.get("methodology_code")
    dataset_code = d.get("dataset_code")
    dataset_version = d.get("dataset_version")
    indicator_code = d.get("indicator_code")

    print("\n" + "=" * 70)
    print("Estado en la BD")
    print("=" * 70)

    m = await pool.fetchrow("SELECT id, code FROM data.methodologies WHERE code=$1", method_code)
    print(f"  methodology {method_code!r}:", "existe" if m else "FALTA")

    if m:
        ds = await pool.fetchrow(
            "SELECT id FROM data.datasets WHERE methodology_id=$1 AND code=$2",
            m["id"], dataset_code,
        )
        print(f"  dataset {dataset_code!r}:", "existe" if ds else "FALTA")

        if ds:
            dv = await pool.fetchrow(
                "SELECT id FROM data.dataset_versions WHERE dataset_id=$1 AND version_label=$2",
                ds["id"], dataset_version,
            )
            print(f"  dataset_version {dataset_version!r}:", "existe" if dv else "FALTA")

        ind = await pool.fetchrow(
            "SELECT id FROM data.indicators WHERE methodology_id=$1 AND code=$2",
            m["id"], indicator_code,
        )
        print(f"  indicator {indicator_code!r}:", "existe" if ind else "FALTA")

    # 3. ¿Existen las geografías únicas en el run?
    geo_rows = await pool.fetch(
        """
        SELECT DISTINCT sr.normalized_data->>'geography_code' AS geo
        FROM ingestion.staging_records sr
        JOIN ingestion.staging_resources sres ON sres.id = sr.staging_resource_id
        JOIN ingestion.etl_runs er ON er.id = sres.etl_run_id
        WHERE er.ingestion_run_id = $1 AND sr.quality_status = 'valid'
          AND sr.normalized_data->>'geography_code' IS NOT NULL
        """,
        UUID(run_id),
    )
    geos = [r["geo"] for r in geo_rows if r["geo"]]
    print(f"\n  geografías únicas en el run: {len(geos)}")
    print(f"  muestras: {geos[:10]}")
    if geos:
        found = await pool.fetch(
            "SELECT code FROM data.geographies WHERE code = ANY($1::text[])",
            geos,
        )
        found_codes = {r["code"] for r in found}
        print(f"  ya en BD: {len(found_codes)} / {len(geos)}")

    # 4. geographic_levels
    levels = await pool.fetch("SELECT id, code, name FROM data.geographic_levels ORDER BY id")
    print("\n  geographic_levels:")
    for r in levels:
        print(f"    {r['id']:3d}  {r['code']:15s}  {r['name']}")

    await pool.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python scripts/diag_publish_cepalstat.py <run_id> [indicator_id]")
        sys.exit(1)
    rid = sys.argv[1]
    ind = int(sys.argv[2]) if len(sys.argv) > 2 else 5554
    asyncio.run(main(rid, ind))