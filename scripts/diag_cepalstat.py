"""Diagnostica por qué un run de CEPALSTAT no tiene staging válido."""

from __future__ import annotations
import asyncio
import sys
from pathlib import Path
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import asyncpg
from app.core.config import load_settings


async def main(run_id: str) -> None:
    settings = load_settings("diag")
    pool = await asyncpg.create_pool(settings.db_dsn, min_size=1, max_size=2)
    rid = UUID(run_id)

    print("=" * 70)
    print(f"DIAGNÓSTICO del run {run_id}")
    print("=" * 70)

    # 1. ¿Existe el run?
    run = await pool.fetchrow(
        "SELECT id, source_id, status, metadata FROM ingestion.ingestion_runs WHERE id=$1",
        rid,
    )
    print("\n[1] ingestion_runs:")
    print("   ", dict(run) if run else "❌ NO EXISTE")

    # 2. ¿Hay etl_runs ligados?
    etl = await pool.fetch(
        "SELECT id, stage, status, input_count, output_count, rejected_count FROM ingestion.etl_runs WHERE ingestion_run_id=$1",
        rid,
    )
    print("\n[2] etl_runs:")
    for r in etl:
        print("   ", dict(r))
    if not etl:
        print("   ❌ Ninguno")

    # 3. ¿Hay staging_resources?
    sres = await pool.fetch(
        """SELECT sres.id, sres.resource_id, sres.parser, sres.records_read, 
                  sres.records_cleaned, sres.records_rejected, sres.status
           FROM ingestion.staging_resources sres
           JOIN ingestion.etl_runs er ON er.id = sres.etl_run_id
           WHERE er.ingestion_run_id = $1""",
        rid,
    )
    print("\n[3] staging_resources:")
    for r in sres:
        print("   ", dict(r))
    if not sres:
        print("   ❌ Ninguno")

    # 4. ¿Hay staging_records? ¿Con qué quality_status?
    stats = await pool.fetch(
        """SELECT sr.quality_status, COUNT(*) AS n
           FROM ingestion.staging_records sr
           JOIN ingestion.staging_resources sres ON sres.id = sr.staging_resource_id
           JOIN ingestion.etl_runs er ON er.id = sres.etl_run_id
           WHERE er.ingestion_run_id = $1
           GROUP BY sr.quality_status""",
        rid,
    )
    print("\n[4] staging_records por quality_status:")
    for r in stats:
        print(f"    {r['quality_status']}: {r['n']}")
    if not stats:
        print("   ❌ Ninguno")

    # 5. Muestra 1-3 registros crudos
    samples = await pool.fetch(
        """SELECT sr.row_number, sr.quality_status, sr.quality_errors,
                  sr.data, sr.normalized_data
           FROM ingestion.staging_records sr
           JOIN ingestion.staging_resources sres ON sres.id = sr.staging_resource_id
           JOIN ingestion.etl_runs er ON er.id = sres.etl_run_id
           WHERE er.ingestion_run_id = $1
           ORDER BY sr.id LIMIT 3""",
        rid,
    )
    print("\n[5] Muestras de staging_records:")
    for r in samples:
        print(f"    --- row {r['row_number']} ({r['quality_status']}) ---")
        print(f"    errors: {r['quality_errors']}")
        print(f"    data:   {str(r['data'])[:300]}")
        print(f"    norm:   {str(r['normalized_data'])[:300]}")

    # 6. La query exacta de get_valid_staging
    count = await pool.fetchval(
        """SELECT COUNT(*)
           FROM ingestion.staging_records sr
           JOIN ingestion.staging_resources sres ON sres.id = sr.staging_resource_id
           JOIN ingestion.etl_runs er ON er.id = sres.etl_run_id
           WHERE er.ingestion_run_id = $1 AND sr.quality_status = 'valid'""",
        rid,
    )
    print(f"\n[6] Registros que get_valid_staging devolvería: {count}")

    await pool.close()


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python scripts/diag_cepalstat.py <run_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))