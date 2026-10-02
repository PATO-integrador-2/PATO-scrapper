from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

class ImportBatch(BaseModel):
    records: list[dict]

def build_router() -> APIRouter:
    r = APIRouter(prefix="/api/v1", tags=["data"])
    @r.get("/methodologies")
    async def methodologies(request: Request):
        return {"data": await request.app.state.data_repo.query_json("SELECT id,code,name,organization,version,publication_date FROM data.methodologies ORDER BY organization,version")}
    @r.get("/datasets")
    async def datasets(request: Request):
        return {"data": await request.app.state.data_repo.query_json("SELECT d.id,d.code,d.name,m.code methodology_code,s.code source_code FROM data.datasets d JOIN data.methodologies m ON m.id=d.methodology_id JOIN data.sources s ON s.id=d.source_id ORDER BY d.id")}
    @r.get("/geographies")
    async def geographies(request: Request, level: str = ""):
        return {"data": await request.app.state.data_repo.query_json("SELECT g.id,g.code,g.name,gl.code level_code,g.parent_id FROM data.geographies g JOIN data.geographic_levels gl ON gl.id=g.geographic_level_id WHERE ($1='' OR gl.code=$1) ORDER BY g.name", level)}
    @r.get("/dimensions")
    async def dimensions(request: Request, methodology: str = ""):
        return {"data": await request.app.state.data_repo.query_json("SELECT d.id,d.code,d.name,m.code methodology_code,d.weight,d.threshold FROM data.dimensions d JOIN data.methodologies m ON m.id=d.methodology_id WHERE ($1='' OR m.code=$1) ORDER BY m.code,d.id", methodology)}
    @r.get("/indicators")
    async def indicators(request: Request, methodology: str = ""):
        return {"data": await request.app.state.data_repo.query_json("SELECT i.id,i.code,i.name,m.code methodology_code,d.code dimension_code,i.unit,i.weight,i.threshold FROM data.indicators i JOIN data.methodologies m ON m.id=i.methodology_id JOIN data.dimensions d ON d.id=i.dimension_id WHERE ($1='' OR m.code=$1) ORDER BY m.code,i.id", methodology)}
    @r.get("/ipm")
    async def observations(request: Request, methodology: str = "", geography: str = "", indicator: str = "", year: str = ""):
        return {"data": await request.app.state.data_repo.query_json("SELECT o.year,o.period,o.value,o.unit,g.code geography_code,g.name geography,i.code indicator_code,i.name indicator,m.code methodology_code FROM data.observations o JOIN data.geographies g ON g.id=o.geography_id JOIN data.indicators i ON i.id=o.indicator_id JOIN data.methodologies m ON m.id=o.methodology_id WHERE ($1='' OR m.code=$1) AND ($2='' OR g.code=$2) AND ($3='' OR i.code=$3) AND ($4='' OR o.year=$4::smallint) ORDER BY o.year,g.code,i.code", methodology, geography, indicator, year)}
    @r.get("/ml/series")
    async def series(request: Request, methodology: str, geography: str, indicator: str):
        return {"data": await request.app.state.data_repo.query_json("SELECT o.year,o.value,g.code geography_code,i.code indicator_code,m.code methodology_code FROM data.observations o JOIN data.geographies g ON g.id=o.geography_id JOIN data.indicators i ON i.id=o.indicator_id JOIN data.methodologies m ON m.id=o.methodology_id WHERE m.code=$1 AND g.code=$2 AND i.code=$3 ORDER BY o.year", methodology, geography, indicator)}
    @r.post("/internal/v1/curated/import")
    async def import_batch(body: ImportBatch, request: Request):
        ok,bad=await request.app.state.data_repo.import_batch(body.records)
        return {"published":ok,"rejected":bad}
    return r
