import asyncio
import hashlib
import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, HttpUrl, field_validator
import httpx

from app.extract.pdf import extract_pdf, chunk_pages
from app.retrieval.embeddings import DOC_PREFIX, QUERY_PREFIX, embed, embed_batch
from app.services.storage import RawObjectStorage


class IngestRequest(BaseModel):
    url: HttpUrl
    title: str | None = None
    organization: str | None = None
    force: bool = False  # re-embeber aunque el PDF (mismo sha256) ya tenga chunks


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    limit: int = Field(default=5, ge=1, le=50)

    @field_validator('query')
    @classmethod
    def query_not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError('query must not be blank')
        return v


def build_knowledge_router(settings):
    r = APIRouter(prefix='/knowledge', tags=['knowledge'])

    @r.post('/ingest')
    async def ingest(body: IngestRequest, request: Request):
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as c:
            resp = await c.get(str(body.url))
            resp.raise_for_status()
            data = resp.content
        if 'pdf' not in resp.headers.get('content-type', '').lower() and not str(body.url).lower().endswith('.pdf'):
            raise HTTPException(415, 'only_pdf_supported')
        h = hashlib.sha256(data).hexdigest()
        repo = request.app.state.knowledge_repo
        existing = None if body.force else await repo.embedded_document(h)
        if existing:
            meta = existing['metadata']
            meta = json.loads(meta) if isinstance(meta, str) else (meta or {})
            return {
                'document_id': str(existing['id']),
                'pages': meta.get('page_count'),
                'chunks': existing['chunks'],
                'sha256': h,
                'object_key': meta.get('object_key'),
                'skipped': True,
            }
        storage = RawObjectStorage(
            settings.object_storage_endpoint,
            settings.object_storage_access_key,
            settings.object_storage_secret_key,
            settings.object_storage_bucket_raw,
            settings.object_storage_region,
        )
        stored = storage.put(data, str(body.url), resp.headers.get('content-type', 'application/pdf'))
        # PyMuPDF es síncrono: en un hilo para no bloquear el event loop con PDFs grandes
        pages, count = await asyncio.to_thread(extract_pdf, data)
        chunks = await asyncio.to_thread(chunk_pages, pages)
        row = await repo.document(
            title=body.title or str(body.url).split('/')[-1],
            organization=body.organization,
            url=str(body.url),
            sha256=h,
            metadata={'page_count': count, 'storage_bucket': stored.bucket, 'object_key': stored.object_key},
        )
        vectors = await embed_batch(settings.ollama_url, settings.embedding_model, [DOC_PREFIX + ch['content'] for ch in chunks])
        for ch, v in zip(chunks, vectors):
            ch['embedding'] = v
        await repo.chunks(row['id'], chunks)
        return {
            'document_id': str(row['id']),
            'pages': count,
            'chunks': len(chunks),
            'sha256': h,
            'object_key': stored.object_key,
        }

    @r.post('/search')
    async def search(body: SearchRequest, request: Request):
        v = await embed(settings.ollama_url, settings.embedding_model, QUERY_PREFIX + body.query)
        rows = await request.app.state.knowledge_repo.search(v, body.query, body.limit)
        return {'results': rows}

    return r