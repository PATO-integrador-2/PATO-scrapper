from __future__ import annotations

from typing import Any

ALIASES = {
    "year": ("year", "ano", "anio", "periodo"),
    "country": ("country", "pais", "pais_code", "country_code"),
    "region": ("region", "region_geografica", "dominio"),
    "department": ("department", "departamento", "depto", "dpto"),
    "ipm": ("ipm", "indice_de_pobreza_multidimensional", "indice_pobreza_multidimensional"),
}


def _first(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        value = row.get(name)
        if value is not None:
            return value
    return None


def canonical_fields(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "year": _first(row, ALIASES["year"]),
        "country": _first(row, ALIASES["country"]),
        "region": _first(row, ALIASES["region"]),
        "department": _first(row, ALIASES["department"]),
        "ipm": _first(row, ALIASES["ipm"]),
    }

