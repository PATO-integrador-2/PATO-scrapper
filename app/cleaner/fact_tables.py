"""
Describe las 9 tablas de hechos de clean_data_db que carga el scraper
(tablas 1 a 6 y 8 a 10 del esquema normalizado; la tabla 7,
ipm_household_departmental, es microdato y tiene su propio mapper en
household_mapper.py).

Cada tabla de hechos guarda los textos repetidos (dominio, país, sexo,
etc.) en una tabla de referencia ref_<concepto> y los apunta desde la
tabla de hechos con la columna <concepto>_id. Por eso un FactTable solo
necesita listar los "conceptos" que referencia: el nombre de la tabla
de referencia, su id y su columna de texto se derivan del concepto
(p. ej. 'domain' -> ref_domain.domain / ref_domain.domain_id).

Las filas se manejan en una forma "lógica" con llaves fijas: 'year',
'value', 'extracted_at' y un texto por cada concepto referenciado.
Tanto las sub-tablas planas del cleaner (llaves en español: anio,
dominio, ...) como los registros de CEPALSTAT se traducen a esa forma
antes de cargarse, así hay un único camino de carga.
"""

import math

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass(frozen=True)
class FactTable:
    table: str
    # Conceptos referenciados (ref_<concepto>), en el orden en que
    # aparecen en la clave UNIQUE de la tabla junto con 'year'.
    refs: tuple[str, ...]
    # Nombre de la columna numérica de la tabla de hechos.
    value_column: str
    # Sub-tabla plana del cleaner que alimenta esta tabla (None si solo
    # se carga desde CEPALSTAT).
    flat_table: str | None = None
    # Mapea columna de la sub-tabla plana -> llave lógica.
    flat_columns: dict[str, str] = field(default_factory=dict)
    # Conceptos cuya columna <concepto>_id admite NULL.
    nullable_refs: tuple[str, ...] = ()


# ----------------------------------------------------------------------------
# Dashboard 1 (fuente: anexos del DANE, vía el cleaner)
# ----------------------------------------------------------------------------

IPM_INCIDENCE = FactTable(
    table="ipm_incidence",
    refs=("domain",),
    value_column="ipm",
    flat_table="ipm_por_dominio",
    flat_columns={"anio": "year", "dominio": "domain", "ipm": "value"},
)

IPM_DEPRIVATIONS_VARIABLE = FactTable(
    table="ipm_deprivations_variable",
    refs=("domain", "variable"),
    value_column="percentage",
    flat_table="privaciones_por_hogar",
    flat_columns={
        "anio": "year",
        "dominio": "domain",
        "variable": "variable",
        "ipm": "value",
    },
)

IPM_DEPRIVATION_PROPORTION = FactTable(
    table="ipm_deprivation_proportion",
    refs=("domain",),
    value_column="percentage",
    flat_table="proporcion_privaciones",
    flat_columns={"anio": "year", "dominio": "domain", "porcentaje": "value"},
)

IPM_CONTRIBUTION_DIMENSION = FactTable(
    table="ipm_contribution_dimension",
    refs=("domain", "dimension"),
    value_column="percentage",
    flat_table="contribuciones_incidencia",
    flat_columns={
        "anio": "year",
        "dominio": "domain",
        "dimension": "dimension",
        "porcentaje": "value",
    },
)

IPM_INCIDENCE_PERSON_SEX = FactTable(
    table="ipm_incidence_person_sex",
    refs=("domain", "sex"),
    value_column="percentage",
    flat_table="incidencia_por_sexo_persona",
    flat_columns={
        "anio": "year",
        "dominio": "domain",
        "sexo": "sex",
        "porcentaje": "value",
    },
)

IPM_INCIDENCE_HOUSEHOLD_HEAD_SEX = FactTable(
    table="ipm_incidence_household_head_sex",
    refs=("domain", "sex"),
    value_column="percentage",
    flat_table="incidencia_por_sexo_jefe_hogar",
    flat_columns={
        "anio": "year",
        "dominio": "domain",
        "sexo": "sex",
        "porcentaje": "value",
    },
)

# ----------------------------------------------------------------------------
# Dashboard 3 (fuente: CEPALSTAT, ver app/cepal_pg_loader.py)
# ----------------------------------------------------------------------------

LATAM_IPM_DEPRIVATION_CONTRIBUTION = FactTable(
    table="latam_ipm_deprivation_contribution",
    refs=("deprivation", "country"),
    value_column="value_percentage",
)

LATAM_IPM_NATIONAL_POPULATION = FactTable(
    table="latam_ipm_national_population",
    refs=("geographic_area", "country", "measurement_type"),
    value_column="value_percentage",
    nullable_refs=("measurement_type",),
)

LATAM_IPM_AGE_GROUP = FactTable(
    table="latam_ipm_age_group",
    refs=("geographic_area", "country", "age_group"),
    value_column="value_percentage",
)


FACT_TABLES: tuple[FactTable, ...] = (
    IPM_INCIDENCE,
    IPM_DEPRIVATIONS_VARIABLE,
    IPM_DEPRIVATION_PROPORTION,
    IPM_CONTRIBUTION_DIMENSION,
    IPM_INCIDENCE_PERSON_SEX,
    IPM_INCIDENCE_HOUSEHOLD_HEAD_SEX,
    LATAM_IPM_DEPRIVATION_CONTRIBUTION,
    LATAM_IPM_NATIONAL_POPULATION,
    LATAM_IPM_AGE_GROUP,
)

# Conceptos que tienen tabla ref_<concepto> con id generado. Es la lista
# blanca que db.py valida antes de armar SQL con el nombre de la tabla.
REF_CONCEPTS: frozenset[str] = frozenset(
    concept for fact in FACT_TABLES for concept in fact.refs
)


def get_fact_table(table: str) -> FactTable:

    for fact in FACT_TABLES:

        if fact.table == table:
            return fact

    raise KeyError(f"No existe la tabla de hechos '{table}'")


def get_fact_table_for_flat(flat_table: str) -> FactTable | None:

    for fact in FACT_TABLES:

        if fact.flat_table == flat_table:
            return fact

    return None


def parse_extraction_date(value) -> date:
    """
    extracted_at es DATE en la base. Acepta el ISO-8601 con hora que
    escribe el exporter ('2026-08-25T00:00:00+00:00'), un date/datetime,
    o None (se usa el día de hoy).
    """

    if isinstance(value, datetime):
        return value.date()

    if isinstance(value, date):
        return value

    if isinstance(value, str) and value.strip():

        try:
            return datetime.fromisoformat(value.strip()).date()

        except ValueError:
            pass

    return date.today()


def build_fact_row(values: dict, fact: FactTable) -> dict | None:
    """
    Valida y normaliza una fila en forma lógica: 'year' y 'value'
    numéricos, un texto no vacío por cada concepto referenciado (salvo
    los de nullable_refs, que pasan como None) y 'extracted_at' como
    date. Devuelve None si la fila no tiene los datos mínimos.
    """

    try:
        year = int(values.get("year"))
        value = float(values.get("value"))

    except (TypeError, ValueError):
        return None

    if not math.isfinite(value):
        return None

    row = {
        "year": year,
        "value": value,
        "extracted_at": parse_extraction_date(values.get("extracted_at")),
    }

    for concept in fact.refs:

        raw = values.get(concept)

        text = str(raw).strip() if raw is not None else ""

        if text == "":

            if concept in fact.nullable_refs:
                row[concept] = None
                continue

            return None

        row[concept] = text

    return row


def flat_row_to_fact_row(row: dict, fact: FactTable) -> dict | None:
    """
    Traduce una fila de sub-tabla plana del cleaner (anio, dominio, ...,
    fuente, fecha_extraccion) a la forma lógica de `fact`.
    """

    values = {
        logical: row.get(flat)
        for flat, logical in fact.flat_columns.items()
    }

    values["extracted_at"] = row.get("fecha_extraccion")

    return build_fact_row(values, fact)
