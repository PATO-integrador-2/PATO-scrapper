from __future__ import annotations
import csv, json, logging
from datetime import datetime, timezone
from pathlib import Path
import psycopg2
from app.cleaner.schema import ALL_TABLES
from app.config import DATA_DATABASE_URL

logger = logging.getLogger("cleaner.loader")
LOG_FILE_NAME = "carga_log.json"
LOADABLE_TABLES = ("ipm_por_dominio","proporcion_privaciones","privaciones_por_hogar","contribuciones_incidencia","incidencia_por_sexo_jefe_hogar","incidencia_por_sexo_persona")


def _read(path: Path) -> list[dict]:
    if not path.exists(): return []
    with path.open("r",encoding="utf-8",newline="") as f: return list(csv.DictReader(f))


def _number(v):
    if v in (None, "", "NA", "N/A", "null"): return None
    return float(str(v).replace("%","").replace(".",".").replace(",","."))


def _year(v):
    try: return int(float(v))
    except (TypeError,ValueError): return None


def _norm(s):
    import unicodedata
    s = str(s or "").strip().lower()
    return "".join(c for c in unicodedata.normalize("NFD",s) if unicodedata.category(c)!="Mn")


def _indicator(table, row):
    if table == "ipm_por_dominio": return ("MPI", "Índice de Pobreza Multidimensional", "mpi", "none", "none")
    if table == "proporcion_privaciones": return ("PRIVATION_SHARE", "Proporción de privaciones", "privation", "none", "none")
    if table == "privaciones_por_hogar":
        v = row.get("variable") or "unknown"; return ("PRIV_"+_norm(v).replace(" ","_"), v, "privation_variable", "none", "none")
    if table == "contribuciones_incidencia":
        v = row.get("dimension") or "unknown"; return ("DIM_"+_norm(v).replace(" ","_"), v, "dimension", "none", "none")
    if table == "incidencia_por_sexo_jefe_hogar": return ("MPI", "Índice de Pobreza Multidimensional", "mpi", "household_head_sex", row.get("sexo") or "none")
    if table == "incidencia_por_sexo_persona": return ("MPI", "Índice de Pobreza Multidimensional", "mpi", "person_sex", row.get("sexo") or "none")
    return None


def _ensure_metadata(cur):
    """
    Asegura que existan la metodología DANE_IPM, la fuente DANE, el dataset
    y la versión. Crea lo que falte. Devuelve (method_id, dataset_id, version_id).
    """
    # --- Metodología ---
    cur.execute("SELECT id FROM methodologies WHERE code='DANE_IPM' ORDER BY id LIMIT 1")
    row = cur.fetchone()
    if row:
        method_id = row[0]
    else:
        cur.execute(
            """INSERT INTO methodologies(code, name, organization, version)
               VALUES ('DANE_IPM', 'IPM Colombia',
                       'Departamento Administrativo Nacional de Estadística',
                       '1.0')
               RETURNING id"""
        )
        method_id = cur.fetchone()[0]

    # --- Fuente ---
    cur.execute("SELECT id FROM sources WHERE code='DANE'")
    row = cur.fetchone()
    if row:
        source_id = row[0]
    else:
        cur.execute(
            """INSERT INTO sources(code, name)
               VALUES ('DANE', 'Departamento Administrativo Nacional de Estadística')
               RETURNING id"""
        )
        source_id = cur.fetchone()[0]

    # --- Dataset ---
    cur.execute(
        """INSERT INTO datasets(methodology_id, source_id, code, name, frequency, unit)
           VALUES (%s, %s, 'DANE_IPM', 'IPM Colombia', 'annual', 'percentage')
           ON CONFLICT (methodology_id, code) DO UPDATE SET name=EXCLUDED.name
           RETURNING id""",
        (method_id, source_id),
    )
    dataset_id = cur.fetchone()[0]

    # --- Versión ---
    version = datetime.now(timezone.utc).strftime("run-%Y%m%d%H%M%S")
    cur.execute(
        "UPDATE dataset_versions SET is_current=FALSE WHERE dataset_id=%s",
        (dataset_id,),
    )
    cur.execute(
        """INSERT INTO dataset_versions(dataset_id, version_label, is_current)
           VALUES (%s, %s, TRUE) RETURNING id""",
        (dataset_id, version),
    )
    version_id = cur.fetchone()[0]
    general_dim_id = _ensure_dimension(cur, method_id, "GENERAL", "General")

    return method_id, dataset_id, version_id, general_dim_id

def _ensure_dimension(cur, method_id: int, code: str = "GENERAL", name: str = "General") -> int:
    """
    Asegura que exista una dimensión `code` para la metodología. La crea
    si falta y devuelve su id. Se usa como dimensión por defecto de los
    indicadores que no tienen una dimensión específica.
    """
    cur.execute(
        "SELECT id FROM dimensions WHERE methodology_id=%s AND code=%s",
        (method_id, code),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """INSERT INTO dimensions(methodology_id, code, name, weight, threshold)
           VALUES (%s, %s, %s, NULL, NULL)
           RETURNING id""",
        (method_id, code, name),
    )
    return cur.fetchone()[0]

def _ensure_geographic_level(cur, code: str, name: str) -> int:
    """
    Asegura que exista un geographic_level con `code`. Lo crea si falta.
    Devuelve su id.
    """
    cur.execute("SELECT id FROM geographic_levels WHERE code=%s", (code,))
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute(
        """INSERT INTO geographic_levels(code, name)
           VALUES (%s, %s)
           RETURNING id""",
        (code, name),
    )
    return cur.fetchone()[0]

def load_tables_from_dir(clean_dir: Path, job_id: str|None=None) -> dict:
    if not DATA_DATABASE_URL:
        raise RuntimeError("DATA_DATABASE_URL no está configurada.")

    mapped = {name: _read(clean_dir/f"{name}.csv") for name in LOADABLE_TABLES}
    report = {}
    conn = psycopg2.connect(DATA_DATABASE_URL)
    with conn.cursor() as _cur:
        _cur.execute("SET search_path TO data, ingestion, public")
    conn.commit()
    try:
        with conn:
            with conn.cursor() as cur:
                method_id, dataset_id, version_id, general_dim_id = _ensure_metadata(cur)
                for table, rows in mapped.items():
                    inserted = rejected = 0
                    for row in rows:
                        year = _year(row.get("anio"))
                        value = _number(row.get("ipm") or row.get("porcentaje"))
                        geo = (row.get("dominio") or "").strip()
                        if not year or not geo or value is None:
                            rejected += 1
                            continue
                        ref = _indicator(table, row)
                        if not ref:
                            rejected += 1
                            continue
                        code, name, category, breakdown_type, breakdown_value = ref

                        cur.execute(
                            """INSERT INTO indicators(methodology_id, dimension_id, code, name, unit)
                               VALUES (%s, %s, %s, %s, 'percentage')
                               ON CONFLICT(methodology_id, code) DO UPDATE SET name=EXCLUDED.name
                               RETURNING id""",
                            (method_id, general_dim_id, code, name),
                        )
                        indicator_id = cur.fetchone()[0]

                        cur.execute("SELECT id FROM geographic_levels WHERE code='domain'")
                        level_id = _ensure_geographic_level(cur, "domain", "Dominio")

                        norm = _norm(geo)
                        cur.execute(
                            """INSERT INTO geographies(geographic_level_id, code, name, normalized_name, country_code)
                               VALUES (%s, %s, %s, %s, 'CO')
                               ON CONFLICT(geographic_level_id, code) DO UPDATE SET name=EXCLUDED.name
                               RETURNING id""",
                            (level_id, norm, geo, norm),
                        )
                        geo_id = cur.fetchone()[0]

                        cur.execute(
                            """INSERT INTO observations(methodology_id, dataset_id, dataset_version_id,
                                                          geography_id, indicator_id, year, period,
                                                          value, unit, quality_flag, source_reference, metadata)
                               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'percentage', 'valid', %s, %s)
                               ON CONFLICT(dataset_version_id, geography_id, indicator_id, year, period)
                               DO UPDATE SET value=EXCLUDED.value,
                                             source_reference=EXCLUDED.source_reference,
                                             metadata=EXCLUDED.metadata""",
                            (method_id, dataset_id, version_id, geo_id, indicator_id,
                             year, str(year), value, "PATO job " + str(job_id or ""),
                             json.dumps({"table": table, "breakdown_type": breakdown_type,
                                         "breakdown_value": breakdown_value})),
                        )
                        inserted += 1
                    report[table] = {"insertadas": inserted, "rechazadas": rejected}

                for spec in ALL_TABLES:
                    if spec.name not in report:
                        rows = _read(clean_dir/f"{spec.name}.csv")
                        report[spec.name] = {
                            "insertadas": 0,
                            "rechazadas": 0,
                            "omitidas": len(rows),
                            "motivo": "Sin mapping de publicación",
                        }
    except Exception:
        logger.exception("Error cargando datos")
        conn.rollback()
        raise
    finally:
        conn.close()

    (clean_dir / LOG_FILE_NAME).write_text(
        json.dumps(
            {"job_id": job_id, "generated_at": datetime.now(timezone.utc).isoformat(), "report": report},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return report
