import httpx
# nomic-embed-text distingue documentos y consultas con estos prefijos (mejora la recuperación)
DOC_PREFIX='search_document: '
QUERY_PREFIX='search_query: '
KEEP_ALIVE='10m'
async def embed(base_url:str,model:str,text:str)->list[float]:
 return (await embed_batch(base_url,model,[text]))[0]
async def embed_batch(base_url:str,model:str,texts:list[str],batch_size:int=32)->list[list[float]]:
 out:list[list[float]]=[]
 async with httpx.AsyncClient(timeout=180) as c:
  for i in range(0,len(texts),batch_size):
   r=await c.post(f'{base_url.rstrip("/")}/api/embed',json={'model':model,'input':texts[i:i+batch_size],'keep_alive':KEEP_ALIVE}); r.raise_for_status(); out.extend(r.json()['embeddings'])
 return out
async def generate(base_url:str,model:str,prompt:str)->str:
 async with httpx.AsyncClient(timeout=180) as c:
  r=await c.post(f'{base_url.rstrip("/")}/api/generate',json={'model':model,'prompt':prompt,'stream':False}); r.raise_for_status(); return r.json()['response']
