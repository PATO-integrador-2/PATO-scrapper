"""
Pruebas de la carga a PostgreSQL (app/cleaner/loader.py, db.py,
fact_tables.py, household_mapper.py).

Las primeras no necesitan base de datos: validan la traducción de filas
a la forma lógica de las tablas de hechos y el mapper de hogares.

Las de integración usan la base real definida en DATABASE_URL (.env) y
se saltan si no hay conexión (ver tests/conftest.py::requires_db). Usan
valores reales (Nacional, Costa Rica, Analfabetismo, ...) y se aíslan
por año: todo se carga con TEST_YEAR, un año sin datos reales, y al
terminar se borran las filas de hechos de ese año (scraper_service
tiene DELETE sobre ellas). Los textos de ref_* son los mismos que ya
usan los datos reales, así que las pruebas no dejan basura ahí.
"""

import json
from datetime import date

import pytest

from app.cleaner.db import get_connection, load_fact_rows, transaction
from app.cleaner.fact_tables import (
    IPM_INCIDENCE,
    LATAM_IPM_AGE_GROUP,
    LATAM_IPM_NATIONAL_POPULATION,
    build_fact_row,
    flat_row_to_fact_row,
    parse_extraction_date,
)
from app.cleaner.household_mapper import (
    DEPRIVATION_COLUMNS,
    REGION_SIN_CLASIFICAR,
    row_to_household,
)
from app.cleaner.loader import load_tables, _write_load_log
from tests.conftest import requires_db


EXTRACTED_AT = "2026-08-25T00:00:00+00:00"

# Tablas de hechos que se desagregan por dominio (ref_domain).
DOMAIN_FACT_TABLES = (
    "ipm_incidence",
    "ipm_deprivations_variable",
    "ipm_deprivation_proportion",
    "ipm_contribution_dimension",
    "ipm_incidence_person_sex",
    "ipm_incidence_household_head_sex",
)

# Año sin datos reales (el IPM empieza en 2010): aísla las pruebas de
# integración y es la llave con la que se limpian las filas que crean.
TEST_YEAR = 1901

TEST_DOMAIN = "Nacional"

# Tablas de hechos que se limpian por año al terminar cada prueba.
LATAM_FACT_TABLES = (
    "latam_ipm_deprivation_contribution",
    "latam_ipm_national_population",
    "latam_ipm_age_group",
)


def _sample_ipm_por_dominio_rows(domain: str) -> list[dict]:

    return [
        {
            "anio": TEST_YEAR,
            "dominio": domain,
            "ipm": 12.5,
            "fuente": "TEST",
            "fecha_extraccion": EXTRACTED_AT,
        }
    ]


def _fetch(sql: str, params: tuple = ()) -> list[tuple]:

    conn = get_connection()

    try:
        with conn.cursor() as cursor:

            cursor.execute(sql, params)

            return cursor.fetchall()

    finally:
        conn.close()


def _delete_test_year_facts() -> None:

    conn = get_connection()

    try:
        with conn:
            with conn.cursor() as cursor:

                for table in (*DOMAIN_FACT_TABLES, *LATAM_FACT_TABLES):

                    cursor.execute(
                        f"DELETE FROM {table} WHERE year = %s", (TEST_YEAR,)
                    )

                cursor.execute(
                    "DELETE FROM ipm_household_departmental WHERE year = %s",
                    (TEST_YEAR,),
                )

    finally:
        conn.close()


@pytest.fixture
def domain():

    _delete_test_year_facts()

    yield TEST_DOMAIN

    _delete_test_year_facts()


def _household_row(**overrides) -> dict:

    row = {
        "anio": TEST_YEAR,
        "region": 1,
        "departamento": 5,
        "personas_hogar": 4,
        "ipm": 0.3,
        "pobre": True,
        "fuente": "TEST",
        "fecha_extraccion": EXTRACTED_AT,
    }

    for flat_column in DEPRIVATION_COLUMNS:
        row[flat_column] = False

    row.update(overrides)

    return row


# ----------------------------------------------------------------------------
# Sin base de datos
# ----------------------------------------------------------------------------


def test_flat_row_to_fact_row_translates_spanish_columns():

    row = flat_row_to_fact_row(
        {
            "anio": 2025,
            "dominio": " Nacional ",
            "ipm": 12.5,
            "fecha_extraccion": EXTRACTED_AT,
        },
        IPM_INCIDENCE,
    )

    assert row == {
        "year": 2025,
        "domain": "Nacional",
        "value": 12.5,
        "extracted_at": date(2026, 8, 25),
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"dominio": None},
        {"dominio": "  "},
        {"anio": None},
        {"ipm": None},
        {"ipm": float("nan")},
        {"ipm": "no es un numero"},
    ],
)
def test_flat_row_to_fact_row_rejects_incomplete_rows(overrides):

    flat_row = {"anio": 2025, "dominio": "Nacional", "ipm": 12.5}

    flat_row.update(overrides)

    assert flat_row_to_fact_row(flat_row, IPM_INCIDENCE) is None


def test_build_fact_row_allows_null_for_nullable_refs_only():

    values = {
        "year": TEST_YEAR,
        "value": 1.5,
        "geographic_area": "Nacional",
        "country": "Costa Rica",
        "measurement_type": None,
    }

    row = build_fact_row(values, LATAM_IPM_NATIONAL_POPULATION)

    assert row is not None
    assert row["measurement_type"] is None

    values["country"] = None

    assert build_fact_row(values, LATAM_IPM_NATIONAL_POPULATION) is None


def test_parse_extraction_date_accepts_iso_and_falls_back_to_today():

    assert parse_extraction_date(EXTRACTED_AT) == date(2026, 8, 25)
    assert parse_extraction_date(date(2026, 1, 2)) == date(2026, 1, 2)
    assert parse_extraction_date(None) == date.today()
    assert parse_extraction_date("no es una fecha") == date.today()


def test_row_to_household_maps_columns_and_defaults_missing_region():

    household = row_to_household(
        _household_row(region=None, priv_analfabetismo=True)
    )

    assert household["region_code"] == REGION_SIN_CLASIFICAR
    assert household["department_code"] == 5
    assert household["household_members"] == 4
    assert household["dep_illiteracy"] is True
    assert household["dep_low_educational_achievement"] is False
    assert household["ipm_score"] == 0.3
    assert household["is_poor"] is True
    assert household["extracted_at"] == date(2026, 8, 25)


@pytest.mark.parametrize(
    "overrides",
    [
        {"departamento": None},
        {"personas_hogar": None},
        {"ipm": None},
        {"pobre": None},
        {"priv_hacinamiento_critico": None},
    ],
)
def test_row_to_household_rejects_incomplete_rows(overrides):

    assert row_to_household(_household_row(**overrides)) is None


# ----------------------------------------------------------------------------
# Integración con la base de datos
# ----------------------------------------------------------------------------


@requires_db
def test_load_tables_inserts_rows(domain):

    report = load_tables(
        {"ipm_por_dominio": _sample_ipm_por_dominio_rows(domain)}
    )

    assert report["ipm_por_dominio"]["insertadas"] == 1
    assert report["ipm_por_dominio"]["rechazadas"] == 0

    rows = _fetch(
        "SELECT year, domain, ipm FROM vw_ipm_incidence WHERE domain = %s",
        (domain,),
    )

    assert len(rows) == 1

    year, name, ipm = rows[0]

    assert (year, name, float(ipm)) == (TEST_YEAR, domain, 12.5)


@requires_db
def test_load_tables_is_idempotent(domain):
    """
    Cargar el mismo lote dos veces no debe duplicar filas ni filas de
    referencia: el UPSERT por la clave UNIQUE de la tabla de hechos
    actualiza en vez de insertar de nuevo, y ref_domain reutiliza el id.
    """

    mapped_tables = {"ipm_por_dominio": _sample_ipm_por_dominio_rows(domain)}

    load_tables(mapped_tables)
    report = load_tables(mapped_tables)

    assert report["ipm_por_dominio"]["insertadas"] == 1

    assert _fetch(
        "SELECT count(*) FROM ref_domain WHERE domain = %s", (domain,)
    )[0][0] == 1

    assert _fetch(
        "SELECT count(*) FROM vw_ipm_incidence WHERE domain = %s", (domain,)
    )[0][0] == 1


@requires_db
def test_load_tables_updates_value_on_reload(domain):

    load_tables({"ipm_por_dominio": _sample_ipm_por_dominio_rows(domain)})

    updated_rows = _sample_ipm_por_dominio_rows(domain)
    updated_rows[0]["ipm"] = 33.3

    load_tables({"ipm_por_dominio": updated_rows})

    rows = _fetch(
        "SELECT ipm FROM vw_ipm_incidence WHERE domain = %s", (domain,)
    )

    assert [float(ipm) for (ipm,) in rows] == [33.3]


@requires_db
def test_load_tables_keeps_last_value_for_duplicate_keys_in_a_batch(domain):
    """
    PostgreSQL rechaza un ON CONFLICT DO UPDATE que toca dos veces la
    misma fila en un mismo INSERT; el lote se deduplica antes.
    """

    rows = _sample_ipm_por_dominio_rows(domain) * 2

    rows[1] = {**rows[1], "ipm": 20.0}

    report = load_tables({"ipm_por_dominio": rows})

    assert "error" not in report["ipm_por_dominio"]

    assert [
        float(ipm)
        for (ipm,) in _fetch(
            "SELECT ipm FROM vw_ipm_incidence WHERE domain = %s", (domain,)
        )
    ] == [20.0]


@requires_db
def test_load_tables_separates_sex_of_person_and_of_household_head(domain):
    """
    Las dos tablas de sexo comparten la misma forma; cada sub-tabla
    plana debe ir a su propia tabla de hechos.
    """

    def flat_row(sex: str, percentage: float) -> dict:

        return {
            "anio": TEST_YEAR,
            "dominio": domain,
            "sexo": sex,
            "porcentaje": percentage,
            "fuente": "TEST",
            "fecha_extraccion": EXTRACTED_AT,
        }

    report = load_tables(
        {
            "incidencia_por_sexo_jefe_hogar": [flat_row("Mujer", 21.4)],
            "incidencia_por_sexo_persona": [flat_row("Hombre", 18.9)],
        }
    )

    assert report["incidencia_por_sexo_jefe_hogar"]["insertadas"] == 1
    assert report["incidencia_por_sexo_persona"]["insertadas"] == 1

    head = _fetch(
        "SELECT sex, percentage FROM vw_ipm_incidence_household_head_sex "
        "WHERE domain = %s",
        (domain,),
    )

    person = _fetch(
        "SELECT sex, percentage FROM vw_ipm_incidence_person_sex "
        "WHERE domain = %s",
        (domain,),
    )

    assert [(sex, float(pct)) for sex, pct in head] == [("Mujer", 21.4)]
    assert [(sex, float(pct)) for sex, pct in person] == [("Hombre", 18.9)]


@requires_db
def test_load_tables_inserts_dimension_and_variable_tables(domain):

    report = load_tables(
        {
            "contribuciones_incidencia": [
                {
                    "anio": TEST_YEAR,
                    "dominio": domain,
                    "dimension": "Condiciones Educativas",
                    "porcentaje": 30.2,
                    "fuente": "TEST",
                    "fecha_extraccion": EXTRACTED_AT,
                }
            ],
            "privaciones_por_hogar": [
                {
                    "anio": TEST_YEAR,
                    "dominio": domain,
                    "variable": "Analfabetismo",
                    "ipm": 13.6,
                    "fuente": "TEST",
                    "fecha_extraccion": EXTRACTED_AT,
                }
            ],
        }
    )

    assert report["contribuciones_incidencia"]["insertadas"] == 1
    assert report["privaciones_por_hogar"]["insertadas"] == 1

    dimension = _fetch(
        "SELECT dimension, percentage FROM vw_ipm_contribution_dimension "
        "WHERE domain = %s",
        (domain,),
    )

    variable = _fetch(
        "SELECT variable, percentage FROM vw_ipm_deprivations_variable "
        "WHERE domain = %s",
        (domain,),
    )

    assert [(d, float(p)) for d, p in dimension] == [
        ("Condiciones Educativas", 30.2)
    ]
    assert [(v, float(p)) for v, p in variable] == [("Analfabetismo", 13.6)]


@requires_db
def test_load_tables_rejects_rows_missing_required_fields():

    mapped_tables = {
        "ipm_por_dominio": [
            {
                "anio": 2025,
                "dominio": None,
                "ipm": 12.5,
                "fuente": "TEST",
                "fecha_extraccion": EXTRACTED_AT,
            }
        ]
    }

    report = load_tables(mapped_tables)

    assert report["ipm_por_dominio"]["insertadas"] == 0
    assert report["ipm_por_dominio"]["rechazadas"] == 1


@requires_db
def test_load_tables_marks_unmapped_tables_as_omitted():
    """
    Las sub-tablas del anexo del DANE sin tabla de hechos (las de
    dashboard_03, que se cargan desde CEPALSTAT) deben reportarse como
    omitidas, no como error.
    """

    mapped_tables = {
        "contribucion_relativa_privaciones": [
            {"anio": TEST_YEAR, "privacion": "Acceso a Internet", "pais": "Chile"},
        ],
    }

    report = load_tables(mapped_tables)

    assert report["contribucion_relativa_privaciones"]["insertadas"] == 0
    assert report["contribucion_relativa_privaciones"]["omitidas"] == 1


@requires_db
def test_load_tables_rolls_back_whole_job_on_db_error(monkeypatch, domain):
    """
    Si una tabla falla a mitad de la transacción del job, ninguna
    fila de ese job debe quedar persistida (atomicidad por job),
    tampoco las filas nuevas de ref_*.
    """

    from app.cleaner import loader as loader_module

    original_load = loader_module.load_fact_rows

    call_count = {"n": 0}

    def failing_load(cursor, fact, rows):

        call_count["n"] += 1

        if call_count["n"] == 2:
            raise RuntimeError("fallo simulado en la segunda tabla")

        return original_load(cursor, fact, rows)

    monkeypatch.setattr(loader_module, "load_fact_rows", failing_load)

    mapped_tables = {
        "ipm_por_dominio": _sample_ipm_por_dominio_rows(domain),
        "proporcion_privaciones": [
            {
                "anio": TEST_YEAR,
                "dominio": domain,
                "porcentaje": 40.0,
                "fuente": "TEST",
                "fecha_extraccion": EXTRACTED_AT,
            }
        ],
    }

    report = load_tables(mapped_tables)

    assert all("error" in entry for entry in report.values())

    for table in ("ipm_incidence", "ipm_deprivation_proportion"):

        assert _fetch(
            f"SELECT count(*) FROM {table} WHERE year = %s", (TEST_YEAR,)
        )[0][0] == 0


@requires_db
def test_load_tables_replaces_household_year_on_reload():
    """
    ipm_household_departmental no tiene UNIQUE: recargar el mismo año
    debe reemplazar sus filas, no duplicarlas.
    """

    mapped_tables = {
        "dashboard_02": [
            _household_row(),
            _household_row(personas_hogar=6, priv_analfabetismo=True),
        ]
    }

    try:
        first = load_tables(mapped_tables)["dashboard_02"]
        second = load_tables(mapped_tables)["dashboard_02"]

        assert first["insertadas"] == 2
        assert second["insertadas"] == 2
        assert second["reemplazadas"] == 2

        assert _fetch(
            "SELECT count(*) FROM ipm_household_departmental WHERE year = %s",
            (TEST_YEAR,),
        )[0][0] == 2

    finally:
        _delete_test_year_facts()


@requires_db
def test_load_tables_rejects_households_with_unknown_department():

    mapped_tables = {"dashboard_02": [_household_row(departamento=999)]}

    report = load_tables(mapped_tables)["dashboard_02"]

    assert report["insertadas"] == 0
    assert report["rechazadas"] == 1

    assert _fetch(
        "SELECT count(*) FROM ipm_household_departmental WHERE year = %s",
        (TEST_YEAR,),
    )[0][0] == 0


@requires_db
def test_load_fact_rows_handles_latam_tables_and_null_measurement_type():
    """
    Camino de CEPALSTAT: filas en forma lógica directo a las tablas
    latam_*. measurement_type puede ser NULL y aun así la clave UNIQUE
    (NULLS NOT DISTINCT) debe evitar duplicados al recargar.
    """

    country = "Costa Rica"

    def rows(value: float) -> list[dict]:

        return [
            build_fact_row(
                {
                    "year": TEST_YEAR,
                    "geographic_area": "Nacional",
                    "country": country,
                    "measurement_type": None,
                    "value": value,
                },
                LATAM_IPM_NATIONAL_POPULATION,
            )
        ]

    def age_rows() -> list[dict]:

        return [
            build_fact_row(
                {
                    "year": TEST_YEAR,
                    "geographic_area": "Rural",
                    "country": country,
                    "age_group": "0 a 14 años",
                    "value": 5.5,
                },
                LATAM_IPM_AGE_GROUP,
            )
        ]

    conn = get_connection()

    _delete_test_year_facts()

    try:
        with transaction(conn):
            with conn.cursor() as cursor:

                load_fact_rows(cursor, LATAM_IPM_NATIONAL_POPULATION, rows(10.0))
                load_fact_rows(cursor, LATAM_IPM_NATIONAL_POPULATION, rows(11.0))
                load_fact_rows(cursor, LATAM_IPM_AGE_GROUP, age_rows())

    finally:
        conn.close()

    try:
        national = _fetch(
            "SELECT measurement_type, value_percentage "
            "FROM vw_latam_ipm_national_population WHERE country = %s",
            (country,),
        )

        age = _fetch(
            "SELECT geographic_area, age_group, value_percentage "
            "FROM vw_latam_ipm_age_group WHERE country = %s",
            (country,),
        )

        assert [(m, float(v)) for m, v in national] == [(None, 11.0)]
        assert [(g, a, float(v)) for g, a, v in age] == [
            ("Rural", "0 a 14 años", 5.5)
        ]

    finally:
        _delete_test_year_facts()


def test_write_load_log_creates_history(tmp_path):

    report = {
        "ipm_por_dominio": {"insertadas": 3, "rechazadas": 0},
    }

    log_path = _write_load_log(tmp_path, job_id="job-123", report=report)

    assert log_path.exists()

    history = json.loads(log_path.read_text(encoding="utf-8"))

    assert len(history) == 1
    assert history[0]["job_id"] == "job-123"
    assert history[0]["total_insertadas"] == 3
    assert history[0]["exito"] is True


def test_write_load_log_appends_to_existing_history(tmp_path):

    report_ok = {"ipm_por_dominio": {"insertadas": 1, "rechazadas": 0}}
    report_fail = {"ipm_por_dominio": {"insertadas": 0, "rechazadas": 1, "error": "boom"}}

    _write_load_log(tmp_path, job_id="job-123", report=report_ok)
    log_path = _write_load_log(tmp_path, job_id="job-123", report=report_fail)

    history = json.loads(log_path.read_text(encoding="utf-8"))

    assert len(history) == 2
    assert history[0]["exito"] is True
    assert history[1]["exito"] is False
