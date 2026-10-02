import hashlib

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, HttpUrl
import httpx

from app.extract.pdf import extract_pdf, chunk_pages
from app.retrieval.embeddings import embed
from app.services.storage import RawObjectStorage


class IngestRequest(BaseModel):
    url: HttpUrl
    title: str | None = None
    organization: str | None = None


class SearchRequest(BaseModel):
    query: str
    limit: int = 5


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
        storage = RawObjectStorage(
            settings.object_storage_endpoint,
            settings.object_storage_access_key,
            settings.object_storage_secret_key,
            settings.object_storage_bucket_raw,
            settings.object_storage_region,
        )
        stored = storage.put(data, str(body.url), resp.headers.get('content-type', 'application/pdf'))
        pages, count = extract_pdf(data)
        chunks = chunk_pages(pages)
        repo = request.app.state.knowledge_repo
        row = await repo.document(
            title=body.title or str(body.url).split('/')[-1],
            organization=body.organization,
            url=str(body.url),
            sha256=h,
            metadata={'page_count': count, 'storage_bucket': stored.bucket, 'object_key': stored.object_key},
        )
        for ch in chunks:
            ch['embedding'] = await embed(settings.ollama_url, settings.embedding_model, ch['content'])
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
        v = await embed(settings.ollama_url, settings.embedding_model, body.query)
        rows = await request.app.state.knowledge_repo.search(v, body.query, body.limit)
        return {'results': rows}

    return r