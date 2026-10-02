from __future__ import annotations
import hashlib
from datetime import UTC, datetime
from pathlib import PurePosixPath
from urllib.parse import urlparse
import boto3
from botocore.client import Config
from app.domain.models import DownloadResult

class RawObjectStorage:
    """Garage S3-compatible object storage. Object keys are content-addressed."""
    def __init__(self, endpoint: str, access_key: str, secret_key: str, bucket: str, region: str='garage') -> None:
        endpoint_url = endpoint if endpoint.startswith('http') else f'http://{endpoint}'
        self.bucket = bucket
        self.client = boto3.client('s3', endpoint_url=endpoint_url, aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name=region, config=Config(signature_version='s3v4'))
    def put(self, content: bytes, url: str, content_type: str) -> DownloadResult:
        digest=hashlib.sha256(content).hexdigest(); basename=PurePosixPath(urlparse(url).path).name or 'download.bin'; safe=basename.replace(' ','_')
        now=datetime.now(UTC); key=f'raw/sha256/{digest[:2]}/{digest}-{safe}'
        try: self.client.head_object(Bucket=self.bucket,Key=key)
        except Exception:
            self.client.put_object(Bucket=self.bucket,Key=key,Body=content,ContentType=content_type or 'application/octet-stream',Metadata={'sha256':digest})
        return DownloadResult(url,200,content_type,safe,len(content),digest,self.bucket,key,now)
    def get_object(self,bucket:str,key:str)->bytes:
        return self.client.get_object(Bucket=bucket,Key=key)['Body'].read()
