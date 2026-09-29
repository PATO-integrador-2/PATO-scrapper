import csv
import json
import logging

from datetime import datetime, timezone
from pathlib import Path

import psycopg2

from app.cleaner.db import (
    delete_households_by_year,
    get_connection,
    get_department_codes,
    insert_household_rows,
    load_fact_rows,
    transaction,
)
from app.cleaner.fact_tables import (
    FACT_TABLES,
    flat_row_to_fact_row,
    get_fact_table_for_flat,
)
from app.cleaner.household_mapper import row_to_household
from app.cleaner.schema import ALL_TABLES, ColumnSpec


logger = logging.getLogger("cleaner.loader")

LOG_FILE_NAME = "carga_log.json"

# Sub-tablas planas que alimentan una tabla de hechos de clean_data_db
# (ver fact_tables.py). El resto se sigue exportando a CSV/Excel pero
# no se carga a PostgreSQL: las de dashboard_03 (contribucion_relativa_
# privaciones, poblacion_pobreza_multidimensional) se cargan desde
# CEPALSTAT (app/cepal_pg_loader.py), no desde los anexos del DANE.
LOADABLE_TABLES = tuple(
    fact.flat_table for fact in FACT_TABLES if fact.flat_table
)

# dashboard_02 es microdato de hogares: va a ipm_household_departmental
# (ver household_mapper.py), no a una tabla de hechos con ref_*.
HOUSEHOLD_TABLE = "dashboard_02"


def _coerce_csv_value(value: str, column: ColumnSpec):

    if value is None or value == "":
        return None

    if column.dtype == "boolean":
        return value.strip().lower() in {"true", "1"}

    if column.dtype == "int":
        return int(float(value))

    if column.dtype == "float":
        return float(value)

    return value


def read_table_csv(csv_path: Path, spec) -> list[dict]:

    if not csv_path.exists():
        return []

    with open(csv_path, "r", newline="", encoding="utf-8") as handle:

        reader = csv.DictReader(handle)

        rows = []

        for raw_row in reader:

            row = {
                column.name: _coerce_csv_value(
                    raw_row.get(column.name), column
                )
                for column in spec.columns
            }

            row["fuente"] = raw_row.get("fuente")
            row["fecha_extraccion"] = raw_row.get("fecha_extraccion")

            rows.append(row)

    return rows


def load_tables_from_dir(clean_dir: Path, job_id: str | None = None) -> dict:
    """
    Lee los CSV generados por clean_job_files() en `clean_dir`
    (uno por sub-tabla) y carga a PostgreSQL las sub-tablas que
    alimentan una tabla de hechos (LOADABLE_TABLES) más el microdato
    de hogares, de forma idempotente. Requiere que DATABASE_URL esté
    configurada en el .env. Registra el resultado en
    <clean_dir>/carga_log.json.
    """

    mapped_tables = {
        spec.name: read_table_csv(clean_dir / f"{spec.name}.csv", spec)
        for spec in ALL_TABLES
    }

    report = load_tables(mapped_tables)

    _write_load_log(clean_dir, job_id, report)

    return report


def load_tables(mapped_tables: dict[str, list[dict]]) -> dict:
    """
    Carga a PostgreSQL las sub-tablas con tabla de hechos (ver
    fact_tables.py) y el microdato de hogares (dashboard_02), todo
    dentro de una única transacción: si una tabla falla, se revierte
    toda la carga del job (atomicidad), evitando estados parciales
    en la base de datos compartida. Requiere que DATABASE_URL esté
    configurada en el .env.
    """

    report = {}

    conn = get_connection()

    try:

        with transaction(conn):

            with conn.cursor() as cursor:

                for table_name in LOADABLE_TABLES:

                    rows = mapped_tables.get(table_name, [])

                    report[table_name] = _load_table(cursor, table_name, rows)

                report[HOUSEHOLD_TABLE] = _load_household_table(
                    cursor,
                    mapped_tables.get(HOUSEHOLD_TABLE, []),
                )

        for spec in ALL_TABLES:

            if spec.name in LOADABLE_TABLES or spec.name == HOUSEHOLD_TABLE:
                continue

            rows = mapped_tables.get(spec.name, [])

            report[spec.name] = {
                "insertadas": 0,
                "rechazadas": 0,
                "omitidas": len(rows),
                "motivo": (
                    "Sin tabla de hechos en PostgreSQL para este origen; "
                    "disponible solo en el CSV/Excel exportado."
                ),
            }

    except (psycopg2.Error, RuntimeError) as exc:

        logger.error("Error cargando el job a PostgreSQL: %s", exc)

        report = {
            table_name: {
                "insertadas": 0,
                "rechazadas": len(mapped_tables.get(table_name, [])),
                "error": str(exc),
            }
            for table_name in (*LOADABLE_TABLES, HOUSEHOLD_TABLE)
        }

    finally:
        conn.close()

    unmatched = mapped_tables.get("sin_clasificar", [])

    if unmatched:

        logger.warning(
            "%s bloques quedaron sin clasificar y no se cargaron "
            "a la base de datos (revisión manual pendiente).",
            len(unmatched),
        )

        report["sin_clasificar"] = {
            "insertadas": 0,
            "rechazadas": len(unmatched),
        }

    return report


def _load_table(cursor, table_name: str, rows: list[dict]) -> dict:

    if not rows:
        return {"insertadas": 0, "rechazadas": 0}

    fact = get_fact_table_for_flat(table_name)

    if fact is None:
        return {
            "insertadas": 0,
            "rechazadas": len(rows),
            "error": f"No hay tabla de hechos para '{table_name}'.",
        }

    fact_rows = []

    rejected = 0

    for row in rows:

        fact_row = flat_row_to_fact_row(row, fact)

        if fact_row is None:
            rejected += 1
            continue

        fact_rows.append(fact_row)

    inserted = load_fact_rows(cursor, fact, fact_rows)

    return {"insertadas": inserted, "rechazadas": rejected}


def _load_household_table(cursor, rows: list[dict]) -> dict:
    """
    Carga dashboard_02 (perfiles de hogar del microdato del DANE) a
    ipm_household_departmental. Se rechazan las filas incompletas y las
    de un departamento que no existe en ref_department.

    La tabla no tiene UNIQUE, así que para que recargar sea idempotente
    se reemplazan por completo los años presentes en el job: se borran
    sus filas y se insertan las nuevas dentro de la misma transacción.
    """

    if not rows:
        return {"insertadas": 0, "rechazadas": 0}

    valid_departments = get_department_codes(cursor)

    households: list[dict] = []

    rejected = 0

    for row in rows:

        household = row_to_household(row)

        if (
            household is None
            or household["department_code"] not in valid_departments
        ):
            rejected += 1
            continue

        households.append(household)

    replaced = delete_households_by_year(
        cursor,
        sorted({household["year"] for household in households}),
    )

    inserted = insert_household_rows(cursor, households)

    return {
        "insertadas": inserted,
        "rechazadas": rejected,
        "reemplazadas": replaced,
    }


def _write_load_log(clean_dir: Path, job_id: str | None, report: dict) -> Path:
    """
    Registra el resultado de la carga en <clean_dir>/carga_log.json,
    acumulando un historial de intentos (uno por ejecución) para que
    quede auditoría local de cuántas filas se insertaron/rechazaron
    y qué errores ocurrieron en cada carga del job.
    """

    log_path = clean_dir / LOG_FILE_NAME

    history = []

    if log_path.exists():

        try:
            history = json.loads(log_path.read_text(encoding="utf-8"))

        except (json.JSONDecodeError, OSError):
            history = []

    total_insertadas = sum(
        table_report.get("insertadas", 0) for table_report in report.values()
    )

    total_rechazadas = sum(
        table_report.get("rechazadas", 0) for table_report in report.values()
    )

    entry = {
        "job_id": job_id,
        "fecha_carga": datetime.now(timezone.utc).isoformat(),
        "total_insertadas": total_insertadas,
        "total_rechazadas": total_rechazadas,
        "exito": total_rechazadas == 0
        and not any("error" in table_report for table_report in report.values()),
        "detalle": report,
    }

    history.append(entry)

    log_path.write_text(
        json.dumps(history, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return log_path
