"""Run inside the Render service shell. Does not print the operator credential."""
import json,os,argparse
from urllib.request import Request,urlopen
from operator_access import credential_path
p=argparse.ArgumentParser();p.add_argument('--acknowledge-provider-access',action='store_true',required=True);p.parse_args()
path=credential_path(os.getenv('CATALOG_PATH','/var/data/alambda-grocery-catalog.sqlite'))
key=path.read_text().strip()
r=urlopen(Request('http://127.0.0.1:'+os.getenv('PORT','3000')+'/api/v1/ingestion/recover',data=b'{"acknowledge":true}',headers={'Content-Type':'application/json','Authorization':'Bearer '+key}),timeout=125)
print(json.dumps(json.load(r)))
