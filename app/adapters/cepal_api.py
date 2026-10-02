from __future__ import annotations

import re
from typing import Any
from urllib.parse import urljoin

import httpx


class CepalStatAPIError(RuntimeError):
    pass


class CepalStatClient:
    """Cliente directo para la API pública de CEPALSTAT."""

    def __init__(self, client: httpx.AsyncClient, base_url: str) -> None:
        self.client = client
        self.base_url = base_url.rstrip("/") + "/"

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = urljoin(self.base_url, path.lstrip("/"))
        response = await self.client.get(url, params=params or {})
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise CepalStatAPIError("CEPALSTAT devolvió una respuesta JSON no válida")
        header = payload.get("header") or {}
        if header.get("success") is False:
            raise CepalStatAPIError(header.get("message") or "CEPALSTAT reportó un error")
        return payload

    async def indicator_data(
        self,
        indicator_id: int,
        *,
        lang: str = "es",
        members: str | None = None,
        include_all_dimensions: int = 1,
        path: int = 0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "lang": lang,
            "format": "json",
            "in": include_all_dimensions,
            "path": path,
        }
        if members:
            params["members"] = members
        return await self._get(f"indicator/{indicator_id}/data", params)

    async def indicator_metadata(self, indicator_id: int, *, lang: str = "es") -> dict[str, Any]:
        return await self._get(f"indicator/{indicator_id}/metadata", {"lang": lang, "format": "json"})

    async def indicator_dimensions(self, indicator_id: int, *, lang: str = "es") -> dict[str, Any]:
        return await self._get(
            f"indicator/{indicator_id}/dimensions",
            {"lang": lang, "format": "json", "in": 1, "path": 1},
        )


def _flatten_members(dimension: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}

    def walk(items: list[dict[str, Any]]) -> None:
        for item in items or []:
            if "id" in item:
                result[int(item["id"])] = item
            walk(item.get("children") or [])

    walk(dimension.get("members") or [])
    return result


def build_dimension_index(dimensions: list[dict[str, Any]]) -> dict[str, dict[int, dict[str, Any]]]:
    return {str(d.get("id")): _flatten_members(d) for d in dimensions}


def infer_year(record: dict[str, Any], dimensions: list[dict[str, Any]]) -> int | None:
    """Busca el año en una dimensión temporal de CEPALSTAT."""
    index = build_dimension_index(dimensions)
    for dim in dimensions:
        name = str(dim.get("name", "")).lower()
        if not any(token in name for token in ("year", "time", "period", "año", "anio")):
            continue
        key = f"dim_{dim.get('id')}"
        member_id = record.get(key)
        member = index.get(str(dim.get("id")), {}).get(int(member_id)) if member_id is not None else None
        text = str((member or {}).get("name", ""))
        match = re.search(r"(?:^|\D)(19\d{2}|20\d{2}|21\d{2})(?:\D|$)", text)
        if match:
            return int(match.group(1))
    # Fallback: algunas respuestas pueden exponer el año directamente.
    for key in ("year", "anio", "año", "period"):
        value = record.get(key)
        match = re.search(r"(19\d{2}|20\d{2}|21\d{2})", str(value or ""))
        if match:
            return int(match.group(1))
    return None


def normalize_indicator_records(indicator_id: int, body: dict[str, Any]) -> list[dict[str, Any]]:
    """Convierte registros CEPALSTAT a un formato de staging común sin perder dimensiones."""
    data = body.get("data") or []
    dimensions = body.get("dimensions") or []
    metadata = body.get("metadata") or {}
    indicator_code = f"CEPALSTAT_{indicator_id}"
    output: list[dict[str, Any]] = []

    for record in data:
        year = infer_year(record, dimensions)
        dimensions_payload = {k: v for k, v in record.items() if str(k).startswith("dim_")}
        output.append(
            {
                "methodology_code": "CEPAL_IPM",
                "dataset_code": f"cepalstat_indicator_{indicator_id}",
                "dataset_version": str(metadata.get("last_update") or "api-current"),
                "indicator_code": indicator_code,
                "geography_code": record.get("iso3"),
                "year": year,
                "period": "",
                "value": record.get("value"),
                "unit": metadata.get("unit"),
                "source_record_id": record.get("source_id"),
                "notes_ids": record.get("notes_ids"),
                "dimensions": dimensions_payload,
                "raw": record,
            }
        )
    return output
