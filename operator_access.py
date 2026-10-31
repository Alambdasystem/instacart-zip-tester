"""Local operator credential; never served to the public frontend or logs."""
import os,secrets
from pathlib import Path
def credential_path(catalog_path):return Path(catalog_path).parent/'instasearch-operator.key'
def ensure_credential(catalog_path):
    path=credential_path(catalog_path)
    try:
        fd=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
        with os.fdopen(fd,'w') as f:f.write(secrets.token_urlsafe(32))
    except FileExistsError:pass
    return path.read_text().strip()
