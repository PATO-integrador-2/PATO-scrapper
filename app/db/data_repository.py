from __future__ import annotations
import hashlib, json
import asyncpg

class DataRepository:
    def __init__(self,pool:asyncpg.Pool): self.pool=pool
    async def query_json(self,query,*args): return [dict(r) for r in await self.pool.fetch(query,*args)]
    async def import_batch(self,records):
        ok=bad=0
        async with self.pool.acquire() as c, c.transaction():
            for x in records:
                required={'methodology_code','dataset_code','indicator_code','year','value','geography_code','dataset_version'}
                if not required.issubset(x): bad+=1; continue
                source_hash=hashlib.sha256(json.dumps(x,sort_keys=True,default=str).encode()).hexdigest()
                try:
                    result=await c.execute("""INSERT INTO data.observations(methodology_id,dataset_id,dataset_version_id,geography_id,indicator_id,year,period,value,unit,source_record_hash,metadata)
                    SELECT m.id,d.id,dv.id,g.id,i.id,$1::smallint,$2,$3::numeric,$4,$5,$6::jsonb
                    FROM data.methodologies m JOIN data.datasets d ON d.methodology_id=m.id AND d.code=$7
                    JOIN data.dataset_versions dv ON dv.dataset_id=d.id AND dv.version_label=$8
                    JOIN data.indicators i ON i.methodology_id=m.id AND i.code=$9
                    JOIN data.geographies g ON g.code=$10 WHERE m.code=$11
                    ON CONFLICT(dataset_version_id,geography_id,indicator_id,year,period) DO UPDATE SET value=EXCLUDED.value,unit=EXCLUDED.unit,source_record_hash=EXCLUDED.source_record_hash,metadata=EXCLUDED.metadata""",x['year'],x.get('period',''),x['value'],x.get('unit'),source_hash,json.dumps(x,default=str),x['dataset_code'],x['dataset_version'],x['indicator_code'],x['geography_code'],x['methodology_code'])
                    ok += int(result.endswith('1')); bad += int(result.endswith('0'))
                except Exception: bad+=1
        return ok,bad
