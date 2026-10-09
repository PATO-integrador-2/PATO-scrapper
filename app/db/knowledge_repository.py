from __future__ import annotations
import json
import asyncpg
from app.cleaner.text_utils import fix_mojibake

class Repository:
    def __init__(self, pool: asyncpg.Pool): self.pool=pool
    async def document(self, **x):
        return await self.pool.fetchrow("""INSERT INTO knowledge.documents(source_id,title,organization,publication_date,document_type,language,url,sha256,content_hash,metadata) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$8,$9::jsonb) ON CONFLICT(sha256) DO UPDATE SET updated_at=now() RETURNING id""",x.get('source_id'),x['title'],x.get('organization'),x.get('publication_date'),x.get('document_type','pdf'),x.get('language','es'),x.get('url'),x['sha256'],json.dumps(x.get('metadata',{})))
    async def embedded_document(self, sha256: str):
        """Devuelve el documento con ese sha256 solo si ya tiene chunks (ingesta completa)."""
        return await self.pool.fetchrow("""SELECT d.id, count(dc.id) chunks, d.metadata FROM knowledge.documents d JOIN knowledge.document_chunks dc ON dc.document_id=d.id WHERE d.sha256=$1 GROUP BY d.id""", sha256)
    async def chunks(self, doc_id, chunks):
        rows = []
        for i, ch in enumerate(chunks):
            content = fix_mojibake(ch['content'])   # ← fix defensivo
            rows.append((
                doc_id, i, content, ch.get('page_number'), ch.get('section'),
                len(content.split()),
                '[' + ','.join(map(str, ch['embedding'])) + ']',
                json.dumps(ch.get('metadata', {})),
            ))
        # executemany en una transacción: un solo viaje por lote y todo-o-nada
        async with self.pool.acquire() as c, c.transaction():
            await c.executemany(
                """INSERT INTO knowledge.document_chunks
                (document_id,chunk_index,content,page_number,section,token_count,embedding,metadata,content_tsv)
                VALUES($1,$2,$3,$4,$5,$6,$7::vector,$8::jsonb,to_tsvector('spanish',$3))
                ON CONFLICT(document_id,chunk_index) DO UPDATE SET
                    content=EXCLUDED.content,
                    embedding=EXCLUDED.embedding,
                    content_tsv=EXCLUDED.content_tsv,
                    metadata=EXCLUDED.metadata""",
                rows,
            )

    async def search(self, embedding: list[float], q: str, limit: int, min_score: float = 0.35):
            vec = '[' + ','.join(map(str, embedding)) + ']'
            rows = await self.pool.fetch(
                """SELECT dc.id, d.id document_id, d.title, d.url, dc.content, dc.page_number,
                        1-(dc.embedding <=> $1::vector) score
                FROM knowledge.document_chunks dc
                JOIN knowledge.documents d ON d.id=dc.document_id
                WHERE dc.embedding IS NOT NULL
                    AND 1-(dc.embedding <=> $1::vector) >= $3
                ORDER BY dc.embedding <=> $1::vector
                LIMIT $2""",
                vec, limit, min_score,
            )
            return [dict(r) for r in rows]
