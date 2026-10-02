"""Inspecciona la respuesta de /indicator/{id}/metadata para ver la estructura real."""

from __future__ import annotations
import asyncio
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

import httpx


async def main(indicator_id: int) -> None:
    base = "https://api-cepalstat.cepal.org/cepalstat/api/v1"

    for endpoint in ("metadata", "dimensions", "data"):
        url = f"{base}/indicator/{indicator_id}/{endpoint}"
        params = {"lang": "es", "format": "json"}
        if endpoint == "data":
            params.update({"in": 1, "path": 0})

        print("=" * 80)
        print(f"GET {url}")
        print(f"Params: {params}")
        print("=" * 80)

        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
                r = await c.get(url, params=params)
                r.raise_for_status()
                payload = r.json()

            # Solo la parte útil: header y las primeras claves de body
            print("header:", json.dumps(payload.get("header"), ensure_ascii=False, indent=2)[:500])
            body = payload.get("body")
            if isinstance(body, dict):
                print("body keys:", list(body.keys()))
                print("body (primeras claves):")
                for k in list(body.keys())[:15]:
                    v = body[k]
                    preview = json.dumps(v, ensure_ascii=False, default=str)[:300]
                    print(f"  {k}: {preview}")
            else:
                print("body (tipo):", type(body).__name__)
                print("body:", json.dumps(body, ensure_ascii=False, default=str)[:500])

        except Exception as exc:
            print(f"ERROR: {type(exc).__name__}: {exc}")

        print()


if __name__ == "__main__":
    ind = int(sys.argv[1]) if len(sys.argv) > 1 else 5554
    asyncio.run(main(ind))