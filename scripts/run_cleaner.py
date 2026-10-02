"""
Descarga los .xlsx del run, los limpia con el cleaner local
y publica los resultados a data.observations.

Uso:
    python scripts/run_cleaner.py <run_id>

Ejemplo:
    python scripts/run_cleaner.py df772080-5ec0-4cb4-880f-a1a88b276159
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID

# --- Asegurar que la raíz del proyecto esté en sys.path ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv

# Cargar .env desde la raíz del proyecto
load_dotenv(PROJECT_ROOT / ".env")

import asyncpg

from app.core.config import load_settings
from app.cleaner.clean_job import clean_job_files
from app.cleaner.loader import load_tables_from_dir
from app.services.storage import RawObjectStorage


async def main(run_id: str) -> None:
    settings = load_settings("cleaner-script")
    pool = await asyncpg.create_pool(settings.db_dsn, min_size=1, max_size=4)

    storage = RawObjectStorage(
        settings.object_storage_endpoint,
        settings.object_storage_access_key,
        settings.object_storage_secret_key,
        settings.object_storage_bucket_raw,
        settings.object_storage_region,
    )

    # 1. Listar los recursos tabulares del run
    rows = await pool.fetch(
        """
        SELECT dr.id, dr.url, dr.resource_type, d.storage_bucket, d.object_key, d.file_name
        FROM ingestion.discovered_resources dr
        JOIN ingestion.run_resources rr ON rr.resource_id = dr.id
        JOIN ingestion.downloads d ON d.resource_id = dr.id
        WHERE rr.run_id = $1
          AND dr.resource_type IN ('xlsx', 'xls', 'csv')
        ORDER BY dr.id
        """,
        UUID(run_id),
    )
    print(f"Recursos tabulares a procesar: {len(rows)}")

    # 2. Descargar cada uno a un directorio local
    job_dir = PROJECT_ROOT / "cleaner_jobs" / run_id
    job_dir.mkdir(parents=True, exist_ok=True)

    for r in rows:
        try:
            content = storage.get_object(r["storage_bucket"], r["object_key"])
            target = job_dir / r["file_name"]
            target.write_bytes(content)
            print(f"  OK {r['file_name']} ({len(content)} bytes)")
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL {r['file_name']}: {exc}")

    print(f"\nArchivos en {job_dir}: {len(list(job_dir.iterdir()))}")

    # 3. Correr el cleaner
    print("\n=== Ejecutando cleaner ===")
    report = clean_job_files(job_dir)
    print(f"Bloques detectados: {report['bloques_detectados']}")
    print("Filas por tabla:")
    for table, count in report.get("filas_por_tabla", {}).items():
        print(f"  {table}: {count}")

    # 4. Cargar a data.observations
    print("\n=== Publicando a data.observations ===")
    clean_dir = job_dir / "datos_limpios"
    load_report = load_tables_from_dir(clean_dir, job_id=run_id)
    print("Resultado:")
    for table, counts in load_report.items():
        print(f"  {table}: {counts}")

    await pool.close()
    print("\nListo")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python scripts/run_cleaner.py <run_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))