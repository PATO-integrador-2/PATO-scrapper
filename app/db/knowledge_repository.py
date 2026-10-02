from __future__ import annotations
import json
import asyncpg
from app.cleaner.text_utils import fix_mojibake

class Repository:
    def __init__(self, pool: asyncpg.Pool): self.pool=pool
    async def document(self, **x):
        return await self.pool.fetchrow("""INSERT INTO knowledge.documents(source_id,title,organization,publication_date,document_type,language,url,sha256,content_hash,metadata) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$8,$9::jsonb) ON CONFLICT(sha256) DO UPDATE SET updated_at=now() RETURNING id""",x.get('source_id'),x['title'],x.get('organization'),x.get('publication_date'),x.get('document_type','pdf'),x.get('language','es'),x.get('url'),x['sha256'],json.dumps(x.get('metadata',{})))
    async def chunks(self, doc_id, chunks):
        async with self.pool.acquire() as c:
            for i, ch in enumerate(chunks):
                content = fix_mojibake(ch['content'])   # ← fix defensivo
                await c.execute(
                    """INSERT INTO knowledge.document_chunks
                    (document_id,chunk_index,content,page_number,section,token_count,embedding,metadata,content_tsv)
                    VALUES($1,$2,$3,$4,$5,$6,$7::vector,$8::jsonb,to_tsvector('spanish',$3))
                    ON CONFLICT(document_id,chunk_index) DO UPDATE SET
                        content=EXCLUDED.content,
                        embedding=EXCLUDED.embedding,
                        content_tsv=EXCLUDED.content_tsv,
                        metadata=EXCLUDED.metadata""",
                    doc_id, i, content, ch.get('page_number'), ch.get('section'),
                    len(content.split()),
                    '[' + ','.join(map(str, ch['embedding'])) + ']',
                    json.dumps(ch.get('metadata', {})),
                )

    async def search(self,embedding:list[float],q:str,limit:int):
        vec='['+','.join(map(str,embedding))+']'
        rows=await self.pool.fetch("""SELECT dc.id,d.id document_id,d.title,d.url,dc.content,dc.page_number,1-(dc.embedding <=> $1::vector) score FROM knowledge.document_chunks dc JOIN knowledge.documents d ON d.id=dc.document_id WHERE dc.embedding IS NOT NULL ORDER BY dc.embedding <=> $1::vector LIMIT $2""",vec,limit)
        return [dict(r) for r in rows]
