"""
Reprocesa y recarga TODOS los jobs de downloads/ contra la API local
(clean + clean/load), en orden, para regenerar ipm_household_departmental
con el mapper actual (ver app/cleaner/household_mapper.py).

Es seguro repetirlo: cada carga reemplaza por completo los años que trae
el job (ver app/cleaner/db.py:delete_households_by_year), no duplica
perfiles de hogar.

Uso:
    python scripts/reload_household_data.py
    python scripts/reload_household_data.py --base-url http://127.0.0.1:8000
    python scripts/reload_household_data.py --dry-run
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "downloads"


def _post(base_url: str, path: str) -> tuple[int, dict]:

    request = urllib.request.Request(
        f"{base_url}{path}",
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.status, body

    except urllib.error.HTTPError as exc:
        body = json.loads(exc.read().decode("utf-8"))
        return exc.code, body


def reload_job(base_url: str, job_id: str, dry_run: bool) -> dict:

    if dry_run:
        return {"job_id": job_id, "status": "dry-run"}

    clean_status, clean_body = _post(base_url, f"/jobs/{job_id}/clean")

    if clean_status == 404:
        return {"job_id": job_id, "status": "sin_job", "detalle": clean_body}

    if clean_status == 422:
        return {"job_id": job_id, "status": "sin_tablas", "detalle": clean_body}

    if clean_status != 200:
        return {"job_id": job_id, "status": "error_clean", "detalle": clean_body}

    load_status, load_body = _post(base_url, f"/jobs/{job_id}/clean/load")

    if load_status != 200:
        return {"job_id": job_id, "status": "error_load", "detalle": load_body}

    return {"job_id": job_id, "status": "ok", "reporte": load_body.get("reporte")}


def main() -> None:

    parser = argparse.ArgumentParser(description=__doc__)

    parser.add_argument(
        "--base-url",
        default="http://127.0.0.1:8000",
        help="Base URL de la API del scraper (default: http://127.0.0.1:8000)",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Solo lista los job_id que se procesarían, sin llamar a la API.",
    )

    args = parser.parse_args()

    if not DOWNLOADS_DIR.exists():
        print(f"No existe {DOWNLOADS_DIR}", file=sys.stderr)
        sys.exit(1)

    job_ids = sorted(
        entry.name
        for entry in DOWNLOADS_DIR.iterdir()
        if entry.is_dir()
    )

    print(f"{len(job_ids)} jobs encontrados en {DOWNLOADS_DIR}")

    results = []

    for job_id in job_ids:

        result = reload_job(args.base_url, job_id, args.dry_run)
        results.append(result)

        print(f"  [{result['status']}] {job_id}")

    ok = sum(1 for r in results if r["status"] == "ok")
    sin_tablas = sum(1 for r in results if r["status"] == "sin_tablas")
    errores = sum(1 for r in results if r["status"] in ("error_clean", "error_load"))

    print(
        f"\nResumen: {ok} recargados, "
        f"{sin_tablas} sin tablas tabulares, "
        f"{errores} con error, "
        f"{len(results) - ok - sin_tablas - errores} otros."
    )

    if errores:

        print("\nJobs con error:", file=sys.stderr)

        for r in results:
            if r["status"] in ("error_clean", "error_load"):
                print(f"  {r['job_id']}: {r['detalle']}", file=sys.stderr)

        sys.exit(1)


if __name__ == "__main__":
    main()
