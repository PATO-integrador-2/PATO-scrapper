"""
Carga las 5 hojas departamentales del anexo del IPM del DANE
(anex-PMultidimensional-Departamental-*.xlsx) a ipm_statistic, con los
breakdown_type 'dept_*' que consultan los endpoints departamentales del
gateway (PATO-api-gateway/internal/services/departmental_queries.go).

Estos datos no pasan por el pipeline de bloques (clean_job) porque cada
hoja tiene una estructura distinta y ningún mapeo de star_schema_mapper
los cubre. Por eso el parseo vive aquí, separado de la escritura.

Uso (prueba en seco por defecto; --apply escribe con UPSERT, no borra):

    python -m app.cleaner.departmental_annex <anexo.xlsx>
    python -m app.cleaner.departmental_annex <anexo.xlsx> --apply

Convenciones:
  IPM_Departamentos            -> MPI,                   dept_area        Total | Cabeceras | Centros poblados y rural disperso
  IPM_Indicadores_Departamento -> 15 privation_variable, dept_area        idem
  Incidencia_Ajustada_IPM      -> ADJUSTED_MPI,          dept_area        Total
  IPM_Sexo                     -> MPI,                   dept_person_sex Hombre | Mujer
  Contribuciones IA_IPM        -> 5 dimension,           dept_contribution none
"""

import logging
import re
import sys
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import openpyxl
import psycopg2.extras

from app.cleaner.db import get_connection


logger = logging.getLogger("cleaner.departmental_annex")

SOURCE_PREFIX = "DANE - anex-PMultidimensional-Departamental"

AREAS = ["Total", "Cabeceras", "Centros poblados y rural disperso"]

SEXES = ["Hombre", "Mujer"]

# La hoja IPM_Indicadores_Departamento rotula "Bogotá"; el nombre oficial
# (y el de la BD) es "Bogotá D.C.".
DEPARTMENT_RENAMES = {"bogota": "Bogotá D.C."}

# Tolerancia para la comparación de IPM_Sexo contra IC_IPM_Sexo.
SEX_TOLERANCE = 0.06

INDICATOR_CODE_MAX_LENGTH = 50


def norm(text) -> str:
    """Minúsculas, sin tildes, con separadores colapsados."""

    text = unicodedata.normalize("NFKD", str(text))
    text = text.encode("ascii", "ignore").decode().lower()

    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def slug(text) -> str:
    """Código de indicator (mayúsculas, guiones bajos, máx. 50 caracteres)."""

    code = re.sub(r"[^A-Z0-9]+", "_", norm(text).upper()).strip("_")

    return code[:INDICATOR_CODE_MAX_LENGTH]


def _year_of(value) -> int | None:

    match = re.match(r"(\d{4})", str(value or ""))

    return int(match.group(1)) if match else None


def _year_columns(row) -> dict[int, int]:
    """{índice_de_columna: año} a partir de una fila de encabezado de años."""

    return {i: _year_of(v) for i, v in enumerate(row) if _year_of(v)}


def _sheet(workbook, name: str):

    return next(ws for ws in workbook.worksheets if ws.title.strip() == name)


def _parse_departments(workbook) -> list[tuple]:

    rows = []

    ws = list(_sheet(workbook, "IPM_Departamentos").iter_rows(values_only=True))

    header = next(i for i, r in enumerate(ws) if r[0] == "Departamento")

    years = _year_columns(ws[header])

    for r in ws[header + 2:]:

        if not r[0] or str(r[0]).startswith(("Fuente", "Datos", "Nota", "Actualizado")):
            continue

        for col, year in years.items():

            for k, area in enumerate(AREAS):

                if r[col + k] is not None:
                    rows.append((r[0], "MPI", year, "dept_area", area, r[col + k]))

    return rows


def _parse_indicators(workbook) -> list[tuple]:

    rows = []

    ws = list(_sheet(workbook, "IPM_Indicadores_Departamento").iter_rows(values_only=True))

    years, dept = None, None

    for r in ws:

        if r[1] == "Variable":
            continue

        if any(_year_of(v) for v in r[2:]) and r[0] in (None, "") and r[1] in (None, ""):
            years = _year_columns(r)
            continue

        if r[1] and years and any(isinstance(v, (int, float)) for v in r[2:]):

            dept = r[0] or dept

            dept = DEPARTMENT_RENAMES.get(norm(dept), dept)

            for col, year in years.items():

                for k, area in enumerate(AREAS):

                    if r[col + k] is not None:
                        rows.append((dept, slug(r[1]), year, "dept_area", area, r[col + k]))

    return rows


def _parse_adjusted(workbook) -> list[tuple]:

    rows = []

    ws = list(_sheet(workbook, "Incidencia_Ajustada_IPM").iter_rows(values_only=True))

    header = next(i for i, r in enumerate(ws) if r[0] == "Departamento")

    years = _year_columns(ws[header])

    for r in ws[header + 1:]:

        if r[0] and isinstance(r[1], (int, float)):

            for col, year in years.items():
                rows.append((r[0], "ADJUSTED_MPI", year, "dept_area", "Total", r[col]))

    return rows


def _parse_sex(workbook) -> tuple[list[tuple], list[tuple]]:
    """
    Usa la columna "Estimación" de IC_IPM_Sexo. La columna 2025 de IPM_Sexo
    no coincide con su propia tabla de intervalos en 16 celdas (Vichada,
    Vaupés, Guainía...), así que no se usa para 2025. Devuelve también las
    diferencias contra IPM_Sexo para revisarlas en el log.
    """

    rows, mismatches = [], []

    sexo = list(_sheet(workbook, "IPM_Sexo").iter_rows(values_only=True))

    ic = list(_sheet(workbook, "IC_IPM_Sexo").iter_rows(values_only=True))

    ic_header = next(i for i, r in enumerate(ic) if r[0] and str(r[0]).startswith("Cifras"))

    ic_years = _year_columns(ic[ic_header])

    sx_header = next(i for i, r in enumerate(sexo) if r[0] and str(r[0]).startswith("Cifras"))

    sx_years = _year_columns(sexo[sx_header])

    sexo_by_dept = {
        norm(r[0]): r
        for r in sexo[sx_header + 3:]
        if r[0] and isinstance(r[1], (int, float))
    }

    for r in ic[ic_header + 3:]:

        if not (r[0] and isinstance(r[1], (int, float))):
            continue

        for col, year in ic_years.items():

            for k, sex in enumerate(SEXES):

                estimate = r[col + 5 * k]

                rows.append((r[0], "MPI", year, "dept_person_sex", sex, estimate))

                sx_col = next(c for c, y in sx_years.items() if y == year) + k

                original = sexo_by_dept[norm(r[0])][sx_col]

                if abs(original - estimate) > SEX_TOLERANCE:
                    mismatches.append((year, r[0], sex, original, estimate))

    return rows, mismatches


def _parse_contributions(workbook) -> list[tuple]:

    rows = []

    ws = list(_sheet(workbook, "Contribuciones IA_IPM").iter_rows(values_only=True))

    dept, years = None, None

    for r in ws:

        text = str(r[0] or "")

        match = re.match(
            r"Contribuciones a la Incidencia Ajustada del IPM (.+?) \d{4}-\d{4}$",
            " ".join(text.split()),
        )

        if match:
            dept = match.group(1)

        elif text.startswith("Dimensi"):
            years = _year_columns(r)

        elif dept and years and isinstance(r[1], (int, float)):

            for col, year in years.items():
                rows.append((dept, slug(r[0]), year, "dept_contribution", "none", r[col]))

    return rows


def parse_annex(path: Path) -> tuple[list[tuple], list[tuple]]:
    """
    Lee el anexo y devuelve (filas, diferencias_sexo). Cada fila es
    (departamento, indicator_code, año, breakdown_type, breakdown_value, valor).
    No toca la base de datos.
    """

    workbook = openpyxl.load_workbook(path, data_only=True)

    sex_rows, sex_mismatches = _parse_sex(workbook)

    rows = (
        _parse_departments(workbook)
        + _parse_indicators(workbook)
        + _parse_adjusted(workbook)
        + sex_rows
        + _parse_contributions(workbook)
    )

    return rows, sex_mismatches


def _resolve_ids(cursor) -> tuple[dict, dict]:

    cursor.execute("SELECT id, name FROM geographic_area WHERE level = 'departamento'")

    geo = {norm(name): gid for gid, name in cursor.fetchall()}

    # Alias: el anexo y la BD usan nombres distintos para San Andrés.
    geo[norm("San Andrés")] = geo[norm("San Andrés, Providencia y Santa Catalina")]

    cursor.execute("SELECT id, code FROM indicator")

    indicators = {code: iid for iid, code in cursor.fetchall()}

    return geo, indicators


def _ensure_adjusted_indicator(cursor, indicators: dict) -> None:
    """Crea la fila ADJUSTED_MPI si no existe (solo datos, sin tablas nuevas)."""

    if "ADJUSTED_MPI" in indicators:
        return

    cursor.execute(
        """
        INSERT INTO indicator (code, name, category)
        VALUES ('ADJUSTED_MPI', %s, 'adjusted_mpi')
        RETURNING id
        """,
        ("Incidencia Ajustada del IPM (M0)",),
    )

    indicators["ADJUSTED_MPI"] = cursor.fetchone()[0]


def _to_values(rows, geo, indicators, source, now) -> list[tuple]:

    missing_geo = sorted({d for d, *_ in rows if norm(d) not in geo})

    missing_ind = sorted({c for _, c, *_ in rows if c not in indicators})

    if missing_geo:
        raise ValueError(f"departamentos sin match en geographic_area: {missing_geo}")

    if missing_ind:
        raise ValueError(f"indicadores sin match en indicator: {missing_ind}")

    # round(..., 6) quita el ruido binario de Excel (0.027999999999999997 -> 0.028).
    values = [
        (
            geo[norm(d)],
            indicators[c],
            year,
            breakdown_type,
            breakdown_value,
            round(float(value), 6),
            source,
            now,
            now,
        )
        for d, c, year, breakdown_type, breakdown_value, value in rows
    ]

    keys = {(g, i, y, bt, bv) for g, i, y, bt, bv, *_ in values}

    if len(keys) != len(values):
        raise ValueError(f"claves duplicadas en el anexo: {len(values) - len(keys)}")

    return values


def load_departmental_annex(path: Path, apply: bool = False) -> dict:
    """
    Parsea el anexo y, si apply es True, lo escribe en ipm_statistic con
    UPSERT. Sin apply hace un rollback y no escribe nada.
    """

    rows, sex_mismatches = parse_annex(path)

    years = sorted({r[2] for r in rows})

    counts = Counter(
        f"{breakdown_type}/{code if code in ('MPI', 'ADJUSTED_MPI') else 'dimension_o_variable'}"
        for _, code, _, breakdown_type, _, _ in rows
    )

    source = f"{SOURCE_PREFIX} - {path.name}"

    now = datetime.now(timezone.utc)

    conn = get_connection()

    try:

        cursor = conn.cursor()

        geo, indicators = _resolve_ids(cursor)

        _ensure_adjusted_indicator(cursor, indicators)

        values = _to_values(rows, geo, indicators, source, now)

        if sex_mismatches:
            logger.warning(
                "IPM_Sexo difiere de IC_IPM_Sexo en %d celdas (años %s); se usa IC_IPM_Sexo",
                len(sex_mismatches),
                sorted({m[0] for m in sex_mismatches}),
            )

        if apply:

            psycopg2.extras.execute_values(
                cursor,
                """
                INSERT INTO ipm_statistic (
                    geographic_area_id, indicator_id, period,
                    breakdown_type, breakdown_value,
                    value, source, extracted_at, loaded_at
                )
                VALUES %s
                ON CONFLICT (
                    geographic_area_id, indicator_id, period,
                    breakdown_type, breakdown_value
                ) DO UPDATE SET
                    value = EXCLUDED.value,
                    source = EXCLUDED.source,
                    extracted_at = EXCLUDED.extracted_at,
                    loaded_at = EXCLUDED.loaded_at
                """,
                values,
                page_size=1000,
            )

            conn.commit()

        else:
            conn.rollback()

    finally:

        conn.close()

    return {
        "aplicado": apply,
        "filas": len(values),
        "filas_por_tipo": dict(counts),
        "anios": years,
        "diferencias_sexo": len(sex_mismatches),
    }


def main(argv: list[str]) -> int:

    if not argv or argv[0].startswith("--"):
        print("Uso: python -m app.cleaner.departmental_annex <anexo.xlsx> [--apply]")
        return 2

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    report = load_departmental_annex(Path(argv[0]), apply="--apply" in argv)

    print(report)

    if not report["aplicado"]:
        print("Prueba en seco: nada escrito. Agrega --apply para cargar.")

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
