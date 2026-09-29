from __future__ import annotations

import os
from typing import Any

from urllib.parse import urlparse, unquote, parse_qsl

import psycopg2

from app.cepal_dimensions_export import (
    build_records,
    CepalDimensionsError,
)
from app.cleaner.db import load_fact_rows, transaction
from app.cleaner.fact_tables import (
    FactTable,
    LATAM_IPM_AGE_GROUP,
    LATAM_IPM_DEPRIVATION_CONTRIBUTION,
    LATAM_IPM_NATIONAL_POPULATION,
    build_fact_row,
)


class CepalLoadError(Exception):
    """Error al cargar datos de CEPALSTAT a PostgreSQL."""


# indicator_id -> configuración de carga.
#
# "fact" es la tabla de hechos destino (ver app/cleaner/fact_tables.py).
# "dimension_map" traduce el NOMBRE DE DIMENSIÓN tal como lo describe
# CEPALSTAT (clave) a la llave lógica de la fila (valor): 'year' o el
# concepto que referencia la tabla de hechos (p. ej. 'country' ->
# ref_country). Todas las dimensiones son obligatorias salvo los
# nullable_refs de la tabla de hechos.
INDICATOR_TABLE_CONFIG: dict[int, dict[str, Any]] = {
    5554: {
        "fact": LATAM_IPM_DEPRIVATION_CONTRIBUTION,
        "dimension_map": {
            "Country__ESTANDAR": "country",
            "Years__ESTANDAR": "year",
            "Deprivation": "deprivation",
        },
    },
    4079: {
        "fact": LATAM_IPM_NATIONAL_POPULATION,
        "dimension_map": {
            "Country__ESTANDAR": "country",
            "Geographical area": "geographic_area",
            "Years__ESTANDAR": "year",
            "Multidimensional poverty measures": "measurement_type",
        },
    },
    5595: {
        "fact": LATAM_IPM_AGE_GROUP,
        "dimension_map": {
            "Country__ESTANDAR": "country",
            "Geographical area": "geographic_area",
            "Years__ESTANDAR": "year",
            "Age_IPM": "age_group",
        },
    },
}

# Llaves lógicas que son INTEGER en Postgres pero vienen como texto desde
# CEPALSTAT (el nombre del miembro de la dimensión de años, ej. "2008").
_INTEGER_COLUMNS = {"year"}


def _get_connection():

    dsn = os.environ.get("DATABASE_URL")

    if not dsn:

        raise CepalLoadError(
            "DATABASE_URL no está configurada. Definila en tu .env antes "
            "de cargar datos a PostgreSQL."
        )

    # Parseamos nosotros mismos la URL (en vez de pasarle el string crudo a
    # psycopg2/libpq) para poder dar un mensaje de error entendible si el
    # formato está mal armado -- por ejemplo, si el password tiene un "@",
    # ":" o "/" sin URL-encodear, o si quedaron dos hosts pegados por error.
    try:
        parsed = urlparse(dsn)

        if parsed.scheme not in ("postgresql", "postgres"):
            raise ValueError(
                f"esquema '{parsed.scheme}' inválido (debe ser "
                "postgresql:// o postgres://)"
            )

        port = parsed.port  # esto ya valida que sea un entero
        host = parsed.hostname
        dbname = parsed.path.lstrip("/")

        if not host or not dbname:
            raise ValueError("falta el host o el nombre de la base de datos")

        conn_kwargs = {
            "host": host,
            "port": port,
            "dbname": dbname,
            "user": unquote(parsed.username) if parsed.username else None,
            "password": unquote(parsed.password) if parsed.password else None,
        }

        # Reenviamos cualquier parámetro extra de la query string
        # (?sslmode=disable, ?connect_timeout=10, etc.) tal cual a psycopg2.
        conn_kwargs.update(dict(parse_qsl(parsed.query)))

    except ValueError as exc:

        # Mostramos el DSN con el password enmascarado para poder
        # depurarlo sin filtrar la contraseña en logs/respuestas.
        safe_dsn = dsn
        if parsed.password:
            safe_dsn = dsn.replace(parsed.password, "***")

        raise CepalLoadError(
            f"DATABASE_URL no es una cadena de conexión válida: {exc}. "
            f"Valor recibido (password oculto): {safe_dsn!r}. "
            "Formato esperado: "
            "postgresql://usuario:password@host:puerto/basededatos -- si "
            "tu password tiene @, :, / u otros caracteres especiales, deben "
            "ir URL-encoded (por ejemplo @ -> %40, : -> %3A)."
        ) from exc

    try:
        return psycopg2.connect(**conn_kwargs)

    except psycopg2.OperationalError as exc:

        raise CepalLoadError(
            f"No se pudo conectar a PostgreSQL en {host}:{port}/{dbname}: "
            f"{exc}"
        ) from exc


def _get_config(indicator_id: int) -> dict[str, Any]:

    config = INDICATOR_TABLE_CONFIG.get(indicator_id)

    if config is None:

        raise CepalLoadError(
            f"El indicador {indicator_id} no tiene una configuración de "
            f"carga definida. Indicadores soportados: "
            f"{list(INDICATOR_TABLE_CONFIG)}"
        )

    return config


def _build_rows_for_indicator(
    indicator_id: int,
) -> tuple[FactTable, list[dict], int]:
    """
    Descarga y mapea el indicador según su configuración en
    INDICATOR_TABLE_CONFIG, devolviendo (tabla de hechos, filas en forma
    lógica, cantidad de filas rechazadas por datos incompletos o no
    numéricos).
    """

    config = _get_config(indicator_id)

    fact: FactTable = config["fact"]

    dimension_map: dict[str, str] = config["dimension_map"]

    try:
        records = build_records(indicator_id)

    except CepalDimensionsError as exc:

        raise CepalLoadError(str(exc)) from exc

    if not records:

        raise CepalLoadError(
            f"El indicador {indicator_id} no devolvió datos."
        )

    # Si CEPALSTAT cambia el nombre de alguna dimensión, mejor fallar fuerte
    # que descartar todas las filas en silencio.
    dimensiones_disponibles = [
        k for k in records[0]
        if k not in ("value", "source_id", "notes_ids", "iso3")
    ]

    faltantes = [
        dim_name
        for dim_name in dimension_map
        if dim_name not in records[0]
    ]

    if faltantes:

        raise CepalLoadError(
            f"El indicador {indicator_id} no trae las dimensiones "
            f"esperadas {faltantes}. Dimensiones disponibles ahora: "
            f"{dimensiones_disponibles}"
        )

    rows: list[dict] = []

    rejected = 0

    for record in records:

        values: dict[str, Any] = {"value": record.get("value")}

        for dim_name, key in dimension_map.items():

            raw_value = record.get(dim_name)

            if key in _INTEGER_COLUMNS and raw_value is not None:

                try:
                    raw_value = int(raw_value)

                except (TypeError, ValueError):

                    raise CepalLoadError(
                        f"No se pudo convertir '{raw_value}' de la "
                        f"dimensión '{dim_name}' a entero para '{key}'."
                    )

            values[key] = raw_value

        row = build_fact_row(values, fact)

        if row is None:
            rejected += 1
            continue

        rows.append(row)

    return fact, rows, rejected


def load_indicator_to_postgres(indicator_id: int) -> dict[str, Any]:
    """
    Descarga el indicador dado desde CEPALSTAT y lo carga directamente
    en la tabla de hechos de Postgres correspondiente (según
    INDICATOR_TABLE_CONFIG), sin generar ningún CSV intermedio. Es
    idempotente: recargar actualiza los valores de la misma clave.
    """

    fact, rows, rejected = _build_rows_for_indicator(indicator_id)

    conn = _get_connection()

    try:

        with transaction(conn):

            with conn.cursor() as cur:

                inserted = load_fact_rows(cur, fact, rows)

    except psycopg2.Error as exc:

        raise CepalLoadError(
            f"Error insertando en la tabla '{fact.table}': {exc}"
        ) from exc

    finally:

        conn.close()

    return {
        "indicator_id": indicator_id,
        "tabla": fact.table,
        "filas_insertadas": inserted,
        "filas_rechazadas": rejected,
    }
