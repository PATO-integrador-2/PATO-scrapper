import logging

from contextlib import contextmanager

import psycopg2
import psycopg2.extras

from app.config import DATABASE_URL


logger = logging.getLogger("cleaner.db")


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


def get_or_create_geographic_area(
    cursor,
    name: str,
    level: str,
) -> str:
    """
    Resuelve el id de geographic_area por (name, level); lo crea si
    no existe todavía. geographic_area no tiene una restricción
    UNIQUE de negocio, así que la deduplicación la hace la
    aplicación vía SELECT-then-INSERT dentro de la misma transacción.
    """

    cursor.execute(
        "SELECT id FROM geographic_area WHERE name = %s AND level = %s",
        (name, level),
    )

    row = cursor.fetchone()

    if row:
        return row[0]

    cursor.execute(
        "INSERT INTO geographic_area (level, name) "
        "VALUES (%s, %s) RETURNING id",
        (level, name),
    )

    return cursor.fetchone()[0]


def get_or_create_indicator(
    cursor,
    code: str,
    name: str,
    category: str,
) -> str:
    """
    Resuelve el id de indicator por su código único; lo crea si no
    existe. indicator.code sí tiene UNIQUE, así que aquí se puede
    usar ON CONFLICT de forma segura.
    """

    cursor.execute(
        """
        INSERT INTO indicator (code, name, category)
        VALUES (%s, %s, %s)
        ON CONFLICT (code) DO UPDATE SET
            name = EXCLUDED.name,
            category = EXCLUDED.category
        RETURNING id
        """,
        (code, name, category),
    )

    return cursor.fetchone()[0]


def upsert_ipm_statistics(cursor, rows: list[dict]) -> int:
    """
    Inserta/actualiza filas de ipm_statistic, idempotente por la
    clave UNIQUE real de la tabla
    (geographic_area_id, indicator_id, period, breakdown_type,
    breakdown_value).
    """

    if not rows:
        return 0

    insert_sql = """
        INSERT INTO ipm_statistic (
            geographic_area_id, indicator_id, period,
            breakdown_type, breakdown_value,
            value, source, extracted_at
        )
        VALUES %s
        ON CONFLICT (
            geographic_area_id, indicator_id, period,
            breakdown_type, breakdown_value
        ) DO UPDATE SET
            value = EXCLUDED.value,
            source = EXCLUDED.source,
            extracted_at = EXCLUDED.extracted_at,
            loaded_at = now()
    """

    values = [
        (
            row["geographic_area_id"],
            row["indicator_id"],
            row["period"],
            row.get("breakdown_type", "none"),
            row.get("breakdown_value", "none"),
            row["value"],
            row["source"],
            row["extracted_at"],
        )
        for row in rows
    ]

    psycopg2.extras.execute_values(cursor, insert_sql, values)

    return len(rows)


def upsert_rows(
    conn,
    table_name: str,
    rows: list[dict],
    natural_key: tuple[str, ...],
) -> int:
    """
    Inserta `rows` en `table_name`, actualizando la fila existente
    cuando su clave natural (`natural_key`) ya está presente
    (UPSERT idempotente vía ON CONFLICT). Uso genérico para tablas
    planas con clave natural propia; no aplica a ipm_statistic (usa
    upsert_ipm_statistics, que resuelve las FK primero).
    """

    if not rows:
        return 0

    columns = list(rows[0].keys())

    update_columns = [
        column
        for column in columns
        if column not in natural_key
    ]

    insert_sql = (
        f'INSERT INTO {table_name} ({", ".join(columns)}) '
        f'VALUES %s '
        f'ON CONFLICT ({", ".join(natural_key)}) DO UPDATE SET '
        + ", ".join(
            f"{column} = EXCLUDED.{column}"
            for column in update_columns
        )
    )

    values = [
        tuple(row[column] for column in columns)
        for row in rows
    ]

    with conn.cursor() as cursor:

        psycopg2.extras.execute_values(
            cursor,
            insert_sql,
            values,
        )

    return len(rows)


def get_or_create_geographic_area_by_code(
    cursor,
    level: str,
    official_code: str,
    name: str,
    parent_id: str | None = None,
) -> str:
    """
    Resuelve el id de geographic_area por (level, official_code); lo
    crea si no existe. Se usa para jerarquías con código oficial DANE
    (región/departamento del microdato de hogares), separado de
    get_or_create_geographic_area (que resuelve por nombre, usado por
    el esquema estrella de dominios) porque las filas creadas por uno
    y otro camino no comparten la misma clave de búsqueda.
    """

    cursor.execute(
        "SELECT id FROM geographic_area WHERE level = %s AND official_code = %s",
        (level, official_code),
    )

    row = cursor.fetchone()

    if row:
        return row[0]

    cursor.execute(
        "INSERT INTO geographic_area (level, official_code, name, parent_id) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (level, official_code, name, parent_id),
    )

    return cursor.fetchone()[0]


HOUSEHOLD_ID_TAG = "hogar="

HOUSEHOLD_COLUMNS = (
    "geographic_area_id",
    "period",
    "household_size",
    "ipm_value",
    "is_poor",
    "source",
    "extracted_at",
)


def household_source(row: dict) -> str:
    """
    El esquema de household_record no se puede alterar (ni columnas
    nuevas ni borrar filas), así que el id del hogar en la ECV y su
    factor de expansión viajan dentro de `source`:
    '<fuente> | hogar=<DIRECTORIO-SECUENCIA_P-SECUENCIA_ENCUESTA> | fex=<FEX_C>'.
    El id hace idempotente la recarga; el fex permite calcular la
    incidencia oficial (ponderada) con
    split_part(split_part(source, 'fex=', 2), ' ', 1)::numeric.
    """

    parts = [row.get("source") or "DANE"]

    if row.get("survey_household_id"):
        parts.append(f"{HOUSEHOLD_ID_TAG}{row['survey_household_id']}")

    if row.get("expansion_factor") is not None:
        parts.append(f"fex={row['expansion_factor']}")

    return " | ".join(parts)


def _column_types(cursor, table: str) -> dict[str, str]:

    cursor.execute(
        """
        SELECT attname, format_type(atttypid, atttypmod)
        FROM pg_attribute
        WHERE attrelid = %s::regclass AND attnum > 0 AND NOT attisdropped
        """,
        (table,),
    )

    return dict(cursor.fetchall())


def insert_household_records(
    cursor,
    rows: list[dict],
) -> list[str]:
    """
    Escribe los hogares en household_record usando solo UPDATE e
    INSERT (sin DELETE ni cambios de esquema) y devuelve sus ids en el
    mismo orden de `rows`:

    1. Un hogar ya cargado (mismo period + 'hogar=<id>' en source) se
       actualiza en su misma fila: recargar un año es idempotente.
    2. Si no, se reutiliza una fila "vieja" (sin 'hogar=' en source,
       p. ej. los perfiles colapsados por la deduplicación anterior),
       sobrescribiéndola por completo, para que no quede ningún dato
       erróneo en la tabla. Sus 15 household_deprivation se
       sobrescriben después por el UPSERT de privaciones.
    3. Si no quedan filas viejas, se inserta.

    Cada dict de `rows` debe traer: geographic_area_id, period,
    household_size, ipm_value, is_poor, expansion_factor,
    survey_household_id, source, extracted_at.
    """

    if not rows:
        return []

    periods = sorted({row["period"] for row in rows})

    cursor.execute(
        """
        SELECT id, period, split_part(split_part(source, %s, 2), ' ', 1)
        FROM household_record
        WHERE period = ANY(%s) AND source LIKE %s
        """,
        (HOUSEHOLD_ID_TAG, periods, f"%{HOUSEHOLD_ID_TAG}%"),
    )

    loaded = {
        (period, survey_id): record_id
        for record_id, period, survey_id in cursor.fetchall()
    }

    cursor.execute(
        """
        SELECT id FROM household_record
        WHERE source IS NULL OR source NOT LIKE %s
        ORDER BY period DESC, id DESC
        FOR UPDATE
        """,
        (f"%{HOUSEHOLD_ID_TAG}%",),
    )

    reusable = [record_id for (record_id,) in cursor.fetchall()]

    ids: list = [None] * len(rows)
    updates: list[tuple] = []
    inserts: list[tuple[int, tuple]] = []

    for index, row in enumerate(rows):

        values = (
            row["geographic_area_id"],
            row["period"],
            row["household_size"],
            row["ipm_value"],
            row["is_poor"],
            household_source(row),
            row["extracted_at"],
        )

        record_id = loaded.get((row["period"], row.get("survey_household_id")))

        if record_id is None and reusable:
            record_id = reusable.pop()

        if record_id is None:
            inserts.append((index, values))
            continue

        ids[index] = record_id
        updates.append((record_id, *values))

    if updates:

        types = _column_types(cursor, "household_record")

        assignments = ", ".join(
            f"{column} = v.{column}::{types[column]}"
            for column in HOUSEHOLD_COLUMNS
        )

        psycopg2.extras.execute_values(
            cursor,
            f"""
            UPDATE household_record AS hr SET {assignments}
            FROM (VALUES %s) AS v(id, {", ".join(HOUSEHOLD_COLUMNS)})
            WHERE hr.id = v.id::{types["id"]}
            """,
            updates,
            page_size=1000,
        )

    if inserts:

        new_ids = psycopg2.extras.execute_values(
            cursor,
            f"""
            INSERT INTO household_record ({", ".join(HOUSEHOLD_COLUMNS)})
            VALUES %s
            RETURNING id
            """,
            [values for _, values in inserts],
            fetch=True,
            page_size=1000,
        )

        for (index, _), (record_id,) in zip(inserts, new_ids):
            ids[index] = record_id

    return ids


def upsert_household_deprivations(
    cursor,
    rows: list[dict],
) -> int:
    """
    Inserta/actualiza filas de household_deprivation, idempotente por
    la clave primaria compuesta (household_record_id, indicator_id).
    """

    if not rows:
        return 0

    insert_sql = """
        INSERT INTO household_deprivation (
            household_record_id, indicator_id, has_deprivation
        )
        VALUES %s
        ON CONFLICT (household_record_id, indicator_id) DO UPDATE SET
            has_deprivation = EXCLUDED.has_deprivation
    """

    values = [
        (
            row["household_record_id"],
            row["indicator_id"],
            row["has_deprivation"],
        )
        for row in rows
    ]

    psycopg2.extras.execute_values(cursor, insert_sql, values)

    return len(rows)
