import os
from dataclasses import dataclass, field

def _parse_deps(raw: str) -> dict[str,str]:
    out={}
    for item in raw.split(','):
        name,sep,addr=item.strip().partition('=')
        if sep and name and addr: out[name]=addr
    return out

def _urls(raw: str): return tuple(x.strip() for x in raw.split(',') if x.strip())

def _ints(raw: str) -> tuple[int,...]:
    values=[]
    for item in raw.split(','):
        item=item.strip()
        if item.isdigit(): values.append(int(item))
    return tuple(values)

def _bool(raw: str, default: bool = False) -> bool:
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "si", "sí"}

@dataclass(frozen=True)
class Settings:
    service_name:str
    version:str='1.0.0'
    log_level:str='INFO'
    ready_deps:dict[str,str]=field(default_factory=dict)
    db_dsn:str=''
    knowledge_db_dsn:str=''
    object_storage_endpoint:str='http://garage:3900'
    object_storage_access_key:str=''
    object_storage_secret_key:str=''
    object_storage_region:str='garage'
    object_storage_bucket_raw:str='ipm-raw'
    object_storage_bucket_staging:str='ipm-staging'
    object_storage_bucket_curated:str='ipm-curated'
    request_timeout:float=30
    max_download_mb:int=250
    user_agent:str='IPM-Platform-DataPlatform/1.0 (+academic-project)'
    discovery_seed_urls:tuple[str,...]=()
    allowed_domains:tuple[str,...]=()
    auto_register_sources:bool=True
    ollama_url:str='http://ollama:11434'
    embedding_model:str='nomic-embed-text'
    embedding_dim:int=768
    cepalstat_api_base_url:str='https://api-cepalstat.cepal.org/cepalstat/api/v1'
    cepalstat_indicators:tuple[int,...]=()

    # --- Crawling ---
    discovery_max_depth:int=2
    discovery_max_pages:int=200
    discovery_rate_limit_seconds:float=1.0
    discovery_keywords:tuple[str,...]=()
    discovery_respect_robots:bool=True
    discovery_same_domain_only:bool=True
    discovery_request_timeout:float=45.0

    # --- Knowledge ingestion desde crawler (cola + worker) ---
    knowledge_ingest_enabled:bool=True
    knowledge_ingest_url:str='http://localhost:8000/knowledge/ingest'
    knowledge_ingest_timeout:float=120.0
    knowledge_worker_enabled:bool=True
    knowledge_worker_interval_seconds:float=30.0
    knowledge_worker_batch_size:int=10
    knowledge_worker_max_attempts:int=3

def _dsn(prefix, default_host, default_name, default_user):
    url=os.getenv(f'{prefix}_DATABASE_URL')
    if url:return url
    h=os.getenv(f'{prefix}_DB_HOST',default_host); p=os.getenv(f'{prefix}_DB_PORT','5432'); n=os.getenv(f'{prefix}_DB_NAME',default_name); u=os.getenv(f'{prefix}_DB_USER',default_user); pw=os.getenv(f'{prefix}_DB_PASSWORD','')
    return f'postgresql://{u}:{pw}@{h}:{p}/{n}'

def load_settings(service_name:str)->Settings:
    return Settings(
      service_name=service_name, version=os.getenv('SERVICE_VERSION','1.0.0'), log_level=os.getenv('LOG_LEVEL','INFO').upper(), ready_deps=_parse_deps(os.getenv('READY_DEPS','')),
      db_dsn=_dsn('DATA_PLATFORM','data-platform-db','data_platform_db','data_platform_user'),
      knowledge_db_dsn=_dsn('KNOWLEDGE','knowledge-db','knowledge_db','knowledge_user'),
      object_storage_endpoint=os.getenv('OBJECT_STORAGE_ENDPOINT','http://garage:3900'), object_storage_access_key=os.getenv('OBJECT_STORAGE_ACCESS_KEY',''), object_storage_secret_key=os.getenv('OBJECT_STORAGE_SECRET_KEY',''), object_storage_region=os.getenv('OBJECT_STORAGE_REGION','garage'),
      object_storage_bucket_raw=os.getenv('OBJECT_STORAGE_BUCKET_RAW','ipm-raw'), object_storage_bucket_staging=os.getenv('OBJECT_STORAGE_BUCKET_STAGING','ipm-staging'), object_storage_bucket_curated=os.getenv('OBJECT_STORAGE_BUCKET_CURATED','ipm-curated'),
      request_timeout=float(os.getenv('INGESTION_REQUEST_TIMEOUT','30')), max_download_mb=int(os.getenv('INGESTION_MAX_DOWNLOAD_MB','250')), user_agent=os.getenv('INGESTION_USER_AGENT','IPM-Platform-DataPlatform/1.0 (+academic-project)'),
      discovery_seed_urls=_urls(os.getenv('DISCOVERY_SEED_URLS','')), allowed_domains=_urls(os.getenv('ALLOWED_DOMAINS','')), auto_register_sources=os.getenv('AUTO_REGISTER_SOURCES','true').lower()=='true',
      ollama_url=os.getenv('OLLAMA_BASE_URL','http://ollama:11434'), embedding_model=os.getenv('EMBEDDING_MODEL','nomic-embed-text'), embedding_dim=int(os.getenv('EMBEDDING_DIM','768')),
      cepalstat_api_base_url=os.getenv('CEPALSTAT_API_BASE_URL','https://api-cepalstat.cepal.org/cepalstat/api/v1'), cepalstat_indicators=_ints(os.getenv('CEPALSTAT_INDICATORS','5554')),
      # --- Crawling ---
      discovery_max_depth=int(os.getenv('DISCOVERY_MAX_DEPTH','2')),
      discovery_max_pages=int(os.getenv('DISCOVERY_MAX_PAGES','200')),
      discovery_rate_limit_seconds=float(os.getenv('DISCOVERY_RATE_LIMIT_SECONDS','1.0')),
      discovery_keywords=_urls(os.getenv('DISCOVERY_KEYWORDS','')),
      discovery_respect_robots=_bool(os.getenv('DISCOVERY_RESPECT_ROBOTS'), True),
      discovery_same_domain_only=_bool(os.getenv('DISCOVERY_SAME_DOMAIN_ONLY'), True),
      discovery_request_timeout=float(os.getenv('DISCOVERY_REQUEST_TIMEOUT','45')),
      # --- Knowledge ---
      knowledge_ingest_enabled=_bool(os.getenv('KNOWLEDGE_INGEST_ENABLED'), True),
      knowledge_ingest_url=os.getenv('KNOWLEDGE_INGEST_URL','http://localhost:8000/knowledge/ingest'),
      knowledge_ingest_timeout=float(os.getenv('KNOWLEDGE_INGEST_TIMEOUT','120')),
      knowledge_worker_enabled=_bool(os.getenv('KNOWLEDGE_WORKER_ENABLED'), True),
      knowledge_worker_interval_seconds=float(os.getenv('KNOWLEDGE_WORKER_INTERVAL_SECONDS','30')),
      knowledge_worker_batch_size=int(os.getenv('KNOWLEDGE_WORKER_BATCH_SIZE','10')),
      knowledge_worker_max_attempts=int(os.getenv('KNOWLEDGE_WORKER_MAX_ATTEMPTS','3')),
    )