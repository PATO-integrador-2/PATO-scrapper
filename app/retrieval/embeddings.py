import httpx
async def embed(base_url:str,model:str,text:str)->list[float]:
 async with httpx.AsyncClient(timeout=90) as c:
  r=await c.post(f'{base_url.rstrip("/")}/api/embeddings',json={'model':model,'prompt':text}); r.raise_for_status(); return r.json()['embedding']
async def generate(base_url:str,model:str,prompt:str)->str:
 async with httpx.AsyncClient(timeout=180) as c:
  r=await c.post(f'{base_url.rstrip("/")}/api/generate',json={'model':model,'prompt':prompt,'stream':False}); r.raise_for_status(); return r.json()['response']
