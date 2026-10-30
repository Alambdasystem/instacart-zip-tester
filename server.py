import json, os, re, secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from pathlib import Path

BASE = Path(__file__).parent
HOSTS = {'development': 'https://connect.dev.instacart.tools', 'production': 'https://connect.instacart.com'}

def validate(data):
    zips = list(dict.fromkeys(data.get('zips', [])))
    foods = list(dict.fromkeys(x.strip() for x in data.get('foods', []) if isinstance(x, str) and x.strip()))
    if not zips or len(zips) > 10 or any(not isinstance(z, str) or not re.fullmatch(r'\d{5}', z) for z in zips):
        raise ValueError('Enter 1–10 valid five-digit US ZIP codes.')
    if not foods or len(foods) > 30 or any(len(x) > 150 for x in foods):
        raise ValueError('Enter 1–30 food items, up to 150 characters each.')
    return zips, foods

def instacart(path, body=None):
    key = os.getenv('INSTACART_API_KEY')
    if not key:
        raise ValueError('Instacart API key required. Search links still work; ZIP lookup and list creation need an approved key.')
    host = HOSTS.get(os.getenv('INSTACART_ENV', 'development'))
    if not host:
        raise ValueError('INSTACART_ENV must be development or production.')
    req = Request(host + path, data=json.dumps(body).encode() if body is not None else None,
                  headers={'Authorization': 'Bearer ' + key, 'Accept': 'application/json', 'Content-Type': 'application/json'})
    try:
        with urlopen(req, timeout=20) as response:
            return json.load(response)
    except HTTPError as e:
        raise ValueError('Instacart returned HTTP %s. Check key permissions, environment, and API access.' % e.code)
    except (URLError, TimeoutError):
        raise ValueError('Instacart connection timed out or failed. Try again later.')

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, code, data):
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self):
        if self.path == '/health':
            return self.reply(200, {'status': 'ok'})
        if self.path == '/api/status':
            return self.reply(200, {'configured': bool(os.getenv('INSTACART_API_KEY')), 'environment': os.getenv('INSTACART_ENV', 'development')})
        if self.path != '/':
            return self.reply(404, {'error': 'Not found'})
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write((BASE / 'index.html').read_bytes())

    def do_POST(self):
        token = os.getenv('TESTER_ACCESS_TOKEN', '')
        if not token:
            return self.reply(503, {'error': 'Set TESTER_ACCESS_TOKEN on the server to enable API calls.'})
        if not secrets.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
            return self.reply(401, {'error': 'Enter the tester access token.'})
        try:
            size = int(self.headers.get('Content-Length', '0'))
            if size < 1 or size > 16000:
                raise ValueError('Invalid request size.')
            data = json.loads(self.rfile.read(size))
            if not isinstance(data, dict):
                raise ValueError('Invalid request body.')
            zips, foods = validate(data)
            if self.path == '/api/retailers':
                rows = []
                for z in zips:
                    try:
                        result = instacart('/idp/v1/retailers?postal_code=' + z + '&country_code=US')
                        rows.append({'zip': z, 'retailers': result.get('retailers', [])})
                    except ValueError as e:
                        rows.append({'zip': z, 'error': str(e)})
                return self.reply(200, {'results': rows})
            if self.path == '/api/list':
                body = {'title': 'Food test shopping list', 'link_type': 'shopping_list', 'line_items': [{'name': f} for f in foods]}
                result = instacart('/idp/v1/products/products_link', body)
                return self.reply(200, {'url': result.get('products_link_url'), 'note': 'ZIP codes are not embedded in this list. Confirm delivery address and retailer on Instacart.'})
            return self.reply(404, {'error': 'Not found'})
        except (ValueError, TypeError, AttributeError) as e:
            return self.reply(400, {'error': str(e) if isinstance(e, ValueError) else 'Invalid input.'})

if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', int(os.getenv('PORT', '3000'))), Handler).serve_forever()
