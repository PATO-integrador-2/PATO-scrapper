"""
Traduce la sub-tabla plana `dashboard_02` (microdato de hogares del
IPM, un perfil de hogar deduplicado por fila) a las columnas de la
tabla ipm_household_departmental de clean_data_db.

Fuente real de los datos: BDATOS-IPM-<año>.zip, publicado con acceso
directo (sin registro) en microdatos.dane.gov.co, hoja
"HOGARES (DEPARTAMENTAL) <año>".

Esta tabla guarda solo los códigos (region_code, department_code). Los
nombres los resuelve la vista vw_ipm_household_departmental con
ref_department (código DANE -> departamento) y ref_region (código de
región -> nombre, versionado por año porque la codificación cambió en
2025), así que el scraper ya no necesita diccionarios de nombres.
"""

from app.cleaner.fact_tables import parse_extraction_date

# Las 15 privaciones del microdato (columnas priv_* de la sub-tabla
# plana) -> columna dep_* de ipm_household_departmental.
DEPRIVATION_COLUMNS: dict[str, str] = {
    "priv_bajo_logro_educativo": "dep_low_educational_achievement",
    "priv_analfabetismo": "dep_illiteracy",
    "priv_inasistencia_escolar": "dep_school_nonattendance",
    "priv_rezago_escolar": "dep_school_lag",
    "priv_atencion_primera_infancia": "dep_early_childhood_care_barriers",
    "priv_trabajo_infantil": "dep_child_labor",
    "priv_no_aseguramiento_salud": "dep_no_health_insurance",
    "priv_barreras_acceso_salud": "dep_health_access_barriers",
    "priv_desempleo_larga_duracion": "dep_long_term_unemployment",
    "priv_tasa_empleo_formal": "dep_informal_employment",
    "priv_no_acceso_agua_mejorada": "dep_no_improved_water_source",
    "priv_inadecuada_eliminacion_excretas": "dep_inadequate_excreta_disposal",
    "priv_material_inadecuado_pisos": "dep_inadequate_floor_material",
    "priv_material_inadecuado_paredes": "dep_inadequate_wall_material",
    "priv_hacinamiento_critico": "dep_critical_overcrowding",
}

# region_code es NOT NULL en la tabla, pero el microdato 2024 no trae la
# columna REGION. Esas filas se guardan con este código, que no existe
# en ref_region: la vista las muestra como 'Sin clasificar' (su
# LEFT JOIN + COALESCE está pensado para eso).
REGION_SIN_CLASIFICAR = 0


def row_to_household(row: dict) -> dict | None:
    """
    Convierte una fila de `dashboard_02` en un dict con las columnas de
    ipm_household_departmental. Devuelve None si falta algún dato
    obligatorio (año, departamento, tamaño del hogar, IPM, pobre o
    cualquiera de las 15 privaciones).
    """

    year = row.get("anio")
    department_code = row.get("departamento")
    household_members = row.get("personas_hogar")
    ipm_score = row.get("ipm")
    is_poor = row.get("pobre")

    if (
        year is None
        or department_code is None
        or household_members is None
        or ipm_score is None
        or is_poor is None
    ):
        return None

    region_code = row.get("region")

    household = {
        "year": int(year),
        "region_code": (
            REGION_SIN_CLASIFICAR if region_code is None else int(region_code)
        ),
        "department_code": int(department_code),
        "household_members": int(household_members),
    }

    for flat_column, db_column in DEPRIVATION_COLUMNS.items():

        has_deprivation = row.get(flat_column)

        if has_deprivation is None:
            return None

        household[db_column] = bool(has_deprivation)

    household["ipm_score"] = float(ipm_score)
    household["is_poor"] = bool(is_poor)
    household["extracted_at"] = parse_extraction_date(
        row.get("fecha_extraccion")
    )

    return household
