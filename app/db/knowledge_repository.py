from __future__ import annotations

import json
import re

import asyncpg

from app.cleaner.text_utils import fix_mojibake


class Repository:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def document(self, **x):
        return await self.pool.fetchrow(
            """
            INSERT INTO knowledge.documents
                (source_id, title, organization, publication_date, document_type,
                 language, url, sha256, content_hash, metadata)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $8, $9::jsonb)
            ON CONFLICT (sha256) DO UPDATE SET
                updated_at   = now(),
                organization = COALESCE(EXCLUDED.organization, knowledge.documents.organization)
            RETURNING id
            """,
            x.get('source_id'), x['title'], x.get('organization'),
            x.get('publication_date'), x.get('document_type', 'pdf'),
            x.get('language', 'es'), x.get('url'), x['sha256'],
            json.dumps(x.get('metadata', {})),
        )

    async def embedded_document(self, sha256: str):
        """Devuelve el documento con ese sha256 solo si ya tiene chunks con embedding."""
        return await self.pool.fetchrow(
            """
            SELECT d.id, d.metadata, count(dc.id) AS chunks
            FROM knowledge.documents d
            JOIN knowledge.document_chunks dc ON dc.document_id = d.id
            WHERE d.sha256 = $1 AND dc.embedding IS NOT NULL
            GROUP BY d.id
            """,
            sha256,
        )

    async def chunks(self, doc_id, chunks):
        rows = []
        for i, ch in enumerate(chunks):
            content = fix_mojibake(ch['content'])
            rows.append((
                doc_id, i, content, ch.get('page_number'), ch.get('section'),
                len(content.split()),
                '[' + ','.join(map(str, ch['embedding'])) + ']',
                json.dumps(ch.get('metadata', {})),
            ))
        async with self.pool.acquire() as c, c.transaction():
            # Evita que queden chunks viejos de ingestas anteriores
            await c.execute(
                "DELETE FROM knowledge.document_chunks WHERE document_id=$1", doc_id
            )
            if rows:
                await c.executemany(
                    """INSERT INTO knowledge.document_chunks
                    (document_id,chunk_index,content,page_number,section,token_count,
                     embedding,metadata,content_tsv)
                    VALUES($1,$2,$3,$4,$5,$6,$7::vector,$8::jsonb,to_tsvector('spanish',$3))""",
                    rows,
                )

    async def search(self, embedding: list[float], q: str, limit: int,
                     min_score: float = 0.5, organization: str | None = None):
        vec = '[' + ','.join(map(str, embedding)) + ']'

        # Solo caracteres de palabra: seguro para to_tsquery. Todos los términos deben aparecer.
        tokens = re.findall(r"\w+", q, flags=re.UNICODE)
        lex_query = " & ".join(tokens)
        # El respaldo vectorial solo para preguntas largas en lenguaje natural
        use_vector_fallback = len(tokens) >= 4

        rows = await self.pool.fetch(
            """
            WITH vec AS (
                SELECT dc.id,
                       ROW_NUMBER() OVER (ORDER BY dc.embedding <=> $1::vector) AS rnk
                FROM knowledge.document_chunks dc
                JOIN knowledge.documents d ON d.id = dc.document_id
                WHERE dc.embedding IS NOT NULL
                  AND ($5::text IS NULL OR lower(d.organization) = lower($5))
                ORDER BY dc.embedding <=> $1::vector
                LIMIT 50
            ),
            lex AS (
                SELECT dc.id,
                       ROW_NUMBER() OVER (ORDER BY ts_rank_cd(dc.content_tsv, tq.query) DESC) AS rnk
                FROM knowledge.document_chunks dc
                JOIN knowledge.documents d ON d.id = dc.document_id
                CROSS JOIN to_tsquery('spanish', $2) AS tq(query)
                WHERE dc.content_tsv @@ tq.query
                  AND ($5::text IS NULL OR lower(d.organization) = lower($5))
                ORDER BY ts_rank_cd(dc.content_tsv, tq.query) DESC
                LIMIT 50
            ),
            fused AS (
                SELECT id, SUM(1.0 / (60 + rnk)) AS rrf
                FROM (SELECT id, rnk FROM vec UNION ALL SELECT id, rnk FROM lex) u
                GROUP BY id
            )
            SELECT dc.id, d.id AS document_id, d.title, d.organization, d.url,
                   dc.content, dc.page_number, dc.section,
                   1 - (dc.embedding <=> $1::vector) AS score,
                   (dc.id IN (SELECT id FROM lex)) AS lexical_match,
                   f.rrf
            FROM fused f
            JOIN knowledge.document_chunks dc ON dc.id = f.id
            JOIN knowledge.documents d ON d.id = dc.document_id
            WHERE dc.id IN (SELECT id FROM lex)
               OR ($6::boolean
                   AND NOT EXISTS (SELECT 1 FROM lex)
                   AND 1 - (dc.embedding <=> $1::vector) >= $4)
            ORDER BY f.rrf DESC
            LIMIT $3
            """,
            vec, lex_query, limit, min_score, organization, use_vector_fallback,
        )
        return [dict(r) for r in rows]