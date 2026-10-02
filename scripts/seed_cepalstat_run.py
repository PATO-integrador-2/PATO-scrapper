"""
Crea la metadata y geografías necesarias para publicar un run de CEPALSTAT.
Lee los valores reales de normalized_data y los crea si faltan.

Uso:
    python scripts/seed_cepalstat_run.py <run_id>

Ejemplo:
    python scripts/seed_cepalstat_run.py 11b185e7-bcf8-4ef1-ac02-7a9770f69666
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import asyncpg
import httpx
from app.core.config import load_settings


# Mapeo ISO3 -> (nombre humano, ISO2 para country_code)
ISO3_TO_INFO: dict[str, tuple[str, str]] = {
    "ARG": ("Argentina", "AR"), "BOL": ("Bolivia", "BO"), "BRA": ("Brasil", "BR"),
    "CHL": ("Chile", "CL"), "COL": ("Colombia", "CO"), "CRI": ("Costa Rica", "CR"),
    "CUB": ("Cuba", "CU"), "DOM": ("República Dominicana", "DO"),
    "ECU": ("Ecuador", "EC"), "SLV": ("El Salvador", "SV"),
    "GTM": ("Guatemala", "GT"), "HTI": ("Haití", "HT"), "HND": ("Honduras", "HN"),
    "MEX": ("México", "MX"), "NIC": ("Nicaragua", "NI"), "PAN": ("Panamá", "PA"),
    "PRY": ("Paraguay", "PY"), "PER": ("Perú", "PE"), "URY": ("Uruguay", "UY"),
    "VEN": ("Venezuela", "VE"), "BLZ": ("Belice", "BZ"),
    "BRB": ("Barbados", "BB"), "GUY": ("Guyana", "GY"), "SUR": ("Surinam", "SR"),
    "TTO": ("Trinidad y Tobago", "TT"), "JAM": ("Jamaica", "JM"),
    "BHS": ("Bahamas", "BS"), "GRD": ("Granada", "GD"),
    "LCA": ("Santa Lucía", "LC"), "VCT": ("San Vicente y las Granadinas", "VC"),
    "DMA": ("Dominica", "DM"), "ATG": ("Antigua y Barbuda", "AG"),
    "KNA": ("San Cristóbal y Nieves", "KN"),
    "PRI": ("Puerto Rico", "PR"),
}


# =========================================================================
# Lectura de staging
# =========================================================================

async def fetch_distinct(pool, run_id: UUID, key: str) -> list[str]:
    """Devuelve los valores únicos no nulos de normalized_data->>key."""
    rows = await pool.fetch(
        f"""
        SELECT DISTINCT sr.normalized_data->>'{key}' AS v
        FROM ingestion.staging_records sr
        JOIN ingestion.staging_resources sres ON sres.id = sr.staging_resource_id
        JOIN ingestion.etl_runs er ON er.id = sres.etl_run_id
        WHERE er.ingestion_run_id = $1
          AND sr.quality_status = 'valid'
          AND sr.normalized_data->>'{key}' IS NOT NULL
        """,
        run_id,
    )
    return [r["v"] for r in rows if r["v"]]


# =========================================================================
# Metadata remota de CEPALSTAT
# =========================================================================

async def fetch_indicator_metadata(indicator_id: int) -> dict:
    """
    Consulta /indicator/{id}/metadata y devuelve un dict con:
    name, unit, theme, area, definition.

    Estructura real de la respuesta (verificada):
        {"body": {"metadata": {"indicator_name": "...", "unit": "..."}}}
    """
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


# =========================================================================
# Helpers de upsert (idempotentes, con UPDATE si ya existen)
# =========================================================================

async def ensure_level(conn, code: str, name: str) -> int:
    row = await conn.fetchrow("SELECT id FROM data.geographic_levels WHERE code=$1", code)
    if row:
        return row["id"]
    return await conn.fetchval(
        "INSERT INTO data.geographic_levels(code, name) VALUES ($1, $2) RETURNING id",
        code, name,
    )


async def ensure_geography(conn, level_id: int, code: str, name: str,
                            country_code: str | None) -> int:
    row = await conn.fetchrow(
        "SELECT id FROM data.geographies WHERE geographic_level_id=$1 AND code=$2",
        level_id, code,
    )
    if row:
        return row["id"]
    return await conn.fetchval(
        """INSERT INTO data.geographies(geographic_level_id, code, name, normalized_name, country_code)
           VALUES ($1, $2, $3, $4, $5) RETURNING id""",
        level_id, code, name, code.lower(), country_code,
    )


async def ensure_methodology(conn, code: str, name: str,
                              organization: str, version: str) -> int:
    row = await conn.fetchrow("SELECT id FROM data.methodologies WHERE code=$1", code)
    if row:
        return row["id"]
    return await conn.fetchval(
        """INSERT INTO data.methodologies(code, name, organization, version)
           VALUES ($1, $2, $3, $4) RETURNING id""",
        code, name, organization, version,
    )


async def ensure_source(conn, code: str, name: str,
                         organization: str, base_url: str) -> int:
    row = await conn.fetchrow("SELECT id FROM data.sources WHERE code=$1", code)
    if row:
        return row["id"]
    return await conn.fetchval(
        """INSERT INTO data.sources(code, name, organization, base_url, country_code)
           VALUES ($1, $2, $3, $4, NULL) RETURNING id""",
        code, name, organization, base_url,
    )


async def ensure_dimension(conn, method_id: int, code: str, name: str) -> int:
    row = await conn.fetchrow(
        "SELECT id FROM data.dimensions WHERE methodology_id=$1 AND code=$2",
        method_id, code,
    )
    if row:
        return row["id"]
    return await conn.fetchval(
        "INSERT INTO data.dimensions(methodology_id, code, name) VALUES ($1, $2, $3) RETURNING id",
        method_id, code, name,
    )


async def ensure_indicator(conn, method_id: int, dim_id: int, code: str,
                            name: str, unit: str | None,
                            description: str | None = None) -> int:
    row = await conn.fetchrow(
        "SELECT id FROM data.indicators WHERE methodology_id=$1 AND code=$2",
        method_id, code,
    )
    if row:
        # Actualiza nombre, unidad y descripción si cambiaron.
        await conn.execute(
            """UPDATE data.indicators
               SET name=$1,
                   unit=$2,
                   description=COALESCE($3, description)
               WHERE id=$4""",
            name, unit, description, row["id"],
        )
        return row["id"]
    return await conn.fetchval(
        """INSERT INTO data.indicators(methodology_id, dimension_id, code, name, unit, description)
           VALUES ($1, $2, $3, $4, $5, $6) RETURNING id""",
        method_id, dim_id, code, name, unit, description,
    )


async def ensure_dataset(conn, method_id: int, source_id: int, code: str,
                          name: str) -> int:
    row = await conn.fetchrow(
        "SELECT id FROM data.datasets WHERE methodology_id=$1 AND code=$2",
        method_id, code,
    )
    if row:
        await conn.execute(
            "UPDATE data.datasets SET name=$1 WHERE id=$2",
            name, row["id"],
        )
        return row["id"]
    return await conn.fetchval(
        """INSERT INTO data.datasets(methodology_id, source_id, code, name, frequency, unit)
           VALUES ($1, $2, $3, $4, 'annual', 'percentage') RETURNING id""",
        method_id, source_id, code, name,
    )


async def ensure_version(conn, dataset_id: int, version_label: str) -> int:
    row = await conn.fetchrow(
        "SELECT id FROM data.dataset_versions WHERE dataset_id=$1 AND version_label=$2",
        dataset_id, version_label,
    )
    if row:
        return row["id"]
    await conn.execute(
        "UPDATE data.dataset_versions SET is_current=FALSE WHERE dataset_id=$1",
        dataset_id,
    )
    return await conn.fetchval(
        """INSERT INTO data.dataset_versions(dataset_id, version_label, is_current)
           VALUES ($1, $2, TRUE) RETURNING id""",
        dataset_id, version_label,
    )


# =========================================================================
# Main
# =========================================================================

async def main(run_id_str: str) -> None:
    settings = load_settings("seed-cepalstat-run")
    pool = await asyncpg.create_pool(settings.db_dsn, min_size=1, max_size=2)
    run_id = UUID(run_id_str)

    # 1. Leer valores únicos del run
    method_codes = await fetch_distinct(pool, run_id, "methodology_code")
    dataset_codes = await fetch_distinct(pool, run_id, "dataset_code")
    dataset_versions = await fetch_distinct(pool, run_id, "dataset_version")
    indicator_codes = await fetch_distinct(pool, run_id, "indicator_code")
    geos = await fetch_distinct(pool, run_id, "geography_code")

    print("=" * 70)
    print(f"Run: {run_id_str}")
    print("=" * 70)
    print("Valores únicos en el run:")
    print(f"  methodology_code: {method_codes}")
    print(f"  dataset_code:     {dataset_codes}")
    print(f"  dataset_version:  {dataset_versions}")
    print(f"  indicator_code:   {indicator_codes}")
    print(f"  geografías:       {len(geos)} -> {geos[:20]}")

    if not (method_codes and dataset_codes and indicator_codes):
        print("\nERROR: faltan claves base en normalized_data.")
        print("Verifica que el /process de CEPALSTAT haya corrido bien.")
        await pool.close()
        return

    method_code = method_codes[0]
    dataset_code = dataset_codes[0]
    dataset_version = dataset_versions[0] if dataset_versions else "api-current"

    async with pool.acquire() as conn:
        async with conn.transaction():
            # 2. Nivel country
            country_level_id = await ensure_level(conn, "country", "País")
            print(f"\n  level 'country' -> id={country_level_id}")

            # 3. Geografías
            created_geos = 0
            for iso3 in geos:
                info = ISO3_TO_INFO.get(iso3)
                if info:
                    name, cc = info
                else:
                    name, cc = iso3, None
                await ensure_geography(conn, country_level_id, iso3, name, cc)
                created_geos += 1
            print(f"  {created_geos} geografías aseguradas")

            # 4. Metodología
            method_id = await ensure_methodology(
                conn, method_code,
                "IPM América Latina (CEPALSTAT)",
                "Comisión Económica para América Latina y el Caribe",
                "1.0",
            )
            print(f"  methodology {method_code!r} -> id={method_id}")

            # 5. Fuente
            source_id = await ensure_source(
                conn, "CEPALSTAT",
                "CEPALSTAT Open Data API",
                "Comisión Económica para América Latina y el Caribe",
                "https://api-cepalstat.cepal.org/cepalstat/api/v1",
            )
            print(f"  source CEPALSTAT -> id={source_id}")

            # 6. Dimensión GENERAL
            dim_id = await ensure_dimension(conn, method_id, "GENERAL", "General")
            print(f"  dimension GENERAL -> id={dim_id}")

            # 7. Indicadores — leer nombre real desde CEPALSTAT
            for indicator_code in indicator_codes:
                # Extraer id numérico: "CEPALSTAT_5554" -> 5554
                try:
                    cepal_id = int(indicator_code.replace("CEPALSTAT_", ""))
                except ValueError:
                    cepal_id = None

                if cepal_id is not None:
                    try:
                        meta = await fetch_indicator_metadata(cepal_id)
                        ind_name = meta["name"] or indicator_code
                        ind_unit = meta["unit"] or "Porcentaje"
                        ind_desc = meta.get("definition")
                        print(f"  metadata CEPALSTAT {cepal_id}:")
                        print(f"    name: {ind_name!r}")
                        print(f"    unit: {ind_unit!r}")
                        print(f"    area: {meta.get('area')!r}")
                    except Exception as exc:  # noqa: BLE001
                        print(f"  ⚠ no se pudo leer metadata de {cepal_id}: {exc}")
                        ind_name = indicator_code
                        ind_unit = "Porcentaje"
                        ind_desc = None
                else:
                    ind_name = indicator_code
                    ind_unit = "Porcentaje"
                    ind_desc = None

                indicator_id = await ensure_indicator(
                    conn, method_id, dim_id,
                    indicator_code, ind_name, ind_unit, ind_desc,
                )
                print(f"  indicator {indicator_code!r} -> id={indicator_id}")

            # 8. Dataset (reusa el nombre del primer indicador)
            first_indicator_name = indicator_codes[0]
            try:
                cepal_id = int(first_indicator_name.replace("CEPALSTAT_", ""))
                meta = await fetch_indicator_metadata(cepal_id)
                ds_name = meta["name"] or dataset_code
            except Exception:  # noqa: BLE001
                ds_name = dataset_code

            dataset_id = await ensure_dataset(
                conn, method_id, source_id, dataset_code, ds_name,
            )
            print(f"  dataset {dataset_code!r} -> id={dataset_id}, name={ds_name!r}")

            # 9. Versión
            version_id = await ensure_version(conn, dataset_id, dataset_version)
            print(f"  dataset_version {dataset_version!r} -> id={version_id}")

    await pool.close()
    print("\n✅ Seed completado. Reintenta el publish.")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python scripts/seed_cepalstat_run.py <run_id>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))