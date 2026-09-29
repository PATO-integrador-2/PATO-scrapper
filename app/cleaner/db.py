import logging

from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from app.cleaner.fact_tables import REF_CONCEPTS, FactTable
from app.config import DATABASE_URL


logger = logging.getLogger("cleaner.db")

HOUSEHOLD_TABLE_NAME = "ipm_household_departmental"


def get_connection():

    if not DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL no está configurada. "
            "Define esa variable en el .env para poder cargar a PostgreSQL."
        )

    return psycopg2.connect(DATABASE_URL)


@contextmanager
def transaction(conn):
    """
    Envuelve un bloque de trabajo en una única transacción: hace
    commit si todo sale bien, o rollback completo si cualquier
    excepción se propaga dentro del bloque (atomicidad por job de
    carga, en vez de dejar la conexión en un estado a medio commitear).
    """

    try:
        yield conn
        conn.commit()

    except Exception:
        conn.rollback()
        raise


def resolve_ref_ids(cursor, concept: str, values) -> dict[str, int]:
    """
    Devuelve {texto: id} para los `values` en ref_<concept>, creando
    las filas que falten. Cada tabla ref_ tiene un id IDENTITY
    smallint, así que primero se consultan los textos existentes y solo
    se insertan los nuevos: un INSERT ... ON CONFLICT DO NOTHING
    consumiría un id de la secuencia por cada texto ya existente en
    cada carga y acabaría desbordando el smallint.
    """

    if concept not in REF_CONCEPTS:
        raise ValueError(f"Tabla de referencia desconocida: ref_{concept}")

    distinct = sorted({value for value in values if value is not None})

    if not distinct:
        return {}

    table = f"ref_{concept}"

    cursor.execute(
        f"SELECT {concept}, {concept}_id FROM {table} "
        f"WHERE {concept} = ANY(%s)",
        (distinct,),
    )

    ids = {text: ref_id for text, ref_id in cursor.fetchall()}

    missing = [value for value in distinct if value not in ids]

    if missing:

        created = psycopg2.extras.execute_values(
            cursor,
            f"INSERT INTO {table} ({concept}) VALUES %s "
            f"RETURNING {concept}, {concept}_id",
            [(value,) for value in missing],
            fetch=True,
        )

        ids.update({text: ref_id for text, ref_id in created})

    return ids


def upsert_fact_rows(
    cursor,
    fact: FactTable,
    rows: list[dict],
    ref_ids: dict[str, dict[str, int]],
) -> int:
    """
    Inserta/actualiza `rows` (forma lógica, ver fact_tables.py) en la
    tabla de hechos, idempotente por su clave UNIQUE
    (year + un id por cada concepto referenciado). `ref_ids` es
    {concepto: {texto: id}} tal como lo devuelve resolve_ref_ids.

    Si la misma clave aparece más de una vez en el lote gana la última:
    PostgreSQL rechaza un ON CONFLICT DO UPDATE que toque dos veces la
    misma fila en un solo INSERT.
    """

    if not rows:
        return 0

    id_columns = [f"{concept}_id" for concept in fact.refs]

    key_columns = ["year", *id_columns]

    columns = [*key_columns, fact.value_column, "extracted_at"]

    values_by_key: dict[tuple, tuple] = {}

    for row in rows:

        ids = tuple(
            None
            if row[concept] is None
            else ref_ids[concept][row[concept]]
            for concept in fact.refs
        )

        key = (row["year"], *ids)

        values_by_key[key] = (*key, row["value"], row["extracted_at"])

    insert_sql = (
        f"INSERT INTO {fact.table} ({', '.join(columns)}) VALUES %s "
        f"ON CONFLICT ({', '.join(key_columns)}) DO UPDATE SET "
        f"{fact.value_column} = EXCLUDED.{fact.value_column}, "
        "extracted_at = EXCLUDED.extracted_at, "
        "loaded_at = CURRENT_DATE"
    )

    psycopg2.extras.execute_values(
        cursor,
        insert_sql,
        list(values_by_key.values()),
        page_size=1000,
    )

    return len(values_by_key)


def load_fact_rows(cursor, fact: FactTable, rows: list[dict]) -> int:
    """
    Carga filas en forma lógica a `fact`: resuelve (o crea) los ids de
    las tablas ref_ que referencia y hace el upsert. Es el único camino
    de carga para las tablas de hechos, tanto desde el cleaner (DANE)
    como desde CEPALSTAT.
    """

    if not rows:
        return 0

    ref_ids = {
        concept: resolve_ref_ids(
            cursor, concept, (row[concept] for row in rows)
        )
        for concept in fact.refs
    }

    return upsert_fact_rows(cursor, fact, rows, ref_ids)


def get_department_codes(cursor) -> set[int]:
    """
    Códigos DANE de ref_department (catálogo cargado por el DBA junto
    con el esquema). Se consulta para rechazar hogares con un
    departamento desconocido en vez de que la FK tumbe todo el job.
    """

    cursor.execute("SELECT department_code FROM public.ref_department")

    return {row[0] for row in cursor.fetchall()}


def delete_households_by_year(cursor, years: list[int]) -> int:
    """
    ipm_household_departmental no tiene UNIQUE (cada fila es un perfil
    de hogar) y por eso no admite UPSERT: para que recargar un año sea
    idempotente se borran sus filas antes de insertarlas de nuevo,
    dentro de la misma transacción de la carga.
    """

    if not years:
        return 0

    cursor.execute(
        f"DELETE FROM public.{HOUSEHOLD_TABLE_NAME} WHERE year = ANY(%s)",
        (list(years),),
    )

    return cursor.rowcount


def insert_household_rows(cursor, rows: list[dict]) -> int:
    """
    Inserta perfiles de hogar en un solo batch. Cada dict de `rows`
    trae ya las columnas de ipm_household_departmental (year,
    region_code, department_code, household_members, dep_*, ipm_score,
    is_poor, extracted_at), todos con las mismas llaves.
    """

    if not rows:
        return 0

    columns = list(rows[0].keys())

    insert_sql = (
        f"INSERT INTO public.{HOUSEHOLD_TABLE_NAME} "
        f"({', '.join(columns)}) VALUES %s"
    )

    psycopg2.extras.execute_values(
        cursor,
        insert_sql,
        [tuple(row[column] for column in columns) for row in rows],
        page_size=1000,
    )

    return len(rows)
