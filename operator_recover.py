"""Run inside the Render service shell. Does not print the operator credential."""
import json,os,argparse
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from operator_access import credential_path
p=argparse.ArgumentParser();p.add_argument('--acknowledge-provider-access',action='store_true',required=True);p.parse_args()
path=credential_path(os.getenv('CATALOG_PATH','/var/data/alambda-grocery-catalog.sqlite'))
key=path.read_text().strip()
try:r=urlopen(Request('http://127.0.0.1:'+os.getenv('PORT','3000')+'/api/v1/ingestion/recover',data=b'{"acknowledge":true}',headers={'Content-Type':'application/json','Authorization':'Bearer '+key}),timeout=125)
except HTTPError as error:r=error
print(json.dumps(json.load(r)))
raise SystemExit(0 if r.status<400 else 1)
