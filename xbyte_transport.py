"""Bounded FoodSpark demo reads. Credentials and raw request URLs never escape.

Only the four supplied demo routes are implemented. Production and scheduled
DoorDash/Uber Eats contracts must be reviewed separately before adding routes.
"""
import json
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import deadline
from connector import ConnectorError
from telemetry import REQUEST_ID, emit

BASE_URL = 'https://foodsparkfastapiframework.xbyte.io'
PATHS = {'/search', '/product_details', '/restaurant_listing', '/restaurant_menu'}
_BUDGET_PATH = None


def enabled(name='enabled'):
    env = 'FOODSPARK_ENABLED' if name == 'enabled' else 'FOODSPARK_RESTAURANTS_ENABLED'
    if env in os.environ:
        return os.environ[env].lower() == 'true'
    try:
        return json.loads(Path(__file__).with_name('xbyte-config.json').read_text()).get(name) is True
    except (OSError, ValueError, TypeError):
        return False


def configure_budget(path):
    global _BUDGET_PATH
    _BUDGET_PATH = str(path)


def _budget():
    try:
        return max(0, min(100, int(os.getenv('FOODSPARK_READ_BUDGET', '0'))))
    except ValueError:
        return 0


def _db():
    path = os.getenv('FOODSPARK_BUDGET_PATH') or _BUDGET_PATH or os.getenv('CATALOG_PATH', '/tmp/alambda-grocery-catalog.sqlite')
    db = sqlite3.connect(path, timeout=5)
    db.execute('CREATE TABLE IF NOT EXISTS xbyte_calls (id INTEGER PRIMARY KEY, at REAL, operation TEXT, status TEXT, request_id TEXT)')
    return db


def status():
    with _db() as db:
        used = db.execute('SELECT COUNT(*) FROM xbyte_calls').fetchone()[0]
    return {'enabled': enabled(), 'restaurants_enabled': enabled('restaurants_enabled'),
            'configured': bool(os.getenv('FOODSPARK_API_KEY')), 'read_budget': _budget(),
            'attempts_in_database': used, 'budget_scope': 'all_attempts_in_this_database',
            'base_url': BASE_URL, 'production_contract_verified': False,
            'background_ingestion': False, 'capture_schedule_verified': False}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _reserve(path):
    with _db() as db:
        db.execute('BEGIN IMMEDIATE')
        used = db.execute('SELECT COUNT(*) FROM xbyte_calls').fetchone()[0]
        if used >= _budget():
            raise ConnectorError('budget_exhausted', 'X-Byte read budget is disabled or exhausted. No upstream request sent.')
        return db.execute('INSERT INTO xbyte_calls(at,operation,status,request_id) VALUES(?,?,?,?)',
                          (time.time(), path, 'started', REQUEST_ID.get())).lastrowid


def _finish(call_id, result):
    with _db() as db:
        db.execute('UPDATE xbyte_calls SET status=? WHERE id=?', (result, call_id))


def _body_error(payload, http_status):
    """Read body-level statuses even when the transport returns HTTP 200."""
    codes = [http_status] if http_status >= 400 else []
    messages = []
    explicit_error = False

    def visit(value, depth=0):
        nonlocal explicit_error
        if depth > 8:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                label = str(key).lower().replace(' ', '_')
                if label in ('status', 'status_code', 'error_code', 'code'):
                    if str(child).isdigit() and int(child) >= 400:
                        codes.append(int(child))
                    elif str(child).lower() in ('error', 'failed', 'failure'):
                        explicit_error = True
                if label == 'success' and child is False:
                    explicit_error = True
                if label in ('error', 'errors') and child not in (None, False, '', [], {}):
                    explicit_error = True
                if label in ('error', 'message', 'error_message', 'detail') and isinstance(child, str):
                    messages.append(child[:1000].casefold())
                # Do not interpret product labels, URLs, or prices as errors.
                if isinstance(child, (dict, list)):
                    visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value[:500]:
                visit(child, depth + 1)

    visit(payload)
    text = ' '.join(messages)
    if any(c in (401, 403) for c in codes) or re.search(r'unauthori[sz]ed|invalid (?:api[ _-]?)?key|invalid credential|authentication failed', text):
        return 'provider_auth', 'X-Byte rejected the server credential.'
    if any(c in (402, 429) for c in codes) or re.search(r'quota|credits? exhausted|insufficient credits|rate limit', text):
        return 'provider_quota', 'X-Byte quota or rate limit requires review; no purchase was attempted.'
    if re.search(r'store not found|not serviceable|no coverage|unsupported location', text):
        return 'no_coverage', 'X-Byte did not resolve the requested store/location. This is not proof of local stock availability.'
    if any(c in (408, 504) for c in codes) or re.search(r'timed? out|timeout', text):
        return 'provider_timeout', 'X-Byte did not complete the read within its time limit.'
    if codes:
        return 'provider_error', 'X-Byte returned an upstream error; no substitute data was returned.'
    if re.search(r'no (?:matching )?(?:results|matches|records|products) (?:found|available)|no match', text):
        return 'no_match', 'X-Byte returned no matching results.'
    if explicit_error:
        return 'provider_error', 'X-Byte returned an upstream error; no substitute data was returned.'
    return None


def request_json(path, params):
    if path not in PATHS or 'api_key' in params:
        raise ConnectorError('unsupported_operation', 'Only documented X-Byte demo reads are supported.')
    if not enabled():
        raise ConnectorError('provider_disabled', 'X-Byte pilot is disabled.')
    key = os.getenv('FOODSPARK_API_KEY')
    if not key:
        raise ConnectorError('not_configured', 'X-Byte demo credential is not configured.')
    try:
        attempts = max(1, min(2, int(os.getenv('FOODSPARK_MAX_ATTEMPTS', '2'))))
    except ValueError:
        attempts = 1
    for attempt in range(attempts):
        deadline.check()
        call_id = _reserve(path)
        started = time.monotonic()
        transport_status = None
        result_code = 'provider_error'
        try:
            request = Request(BASE_URL + path + '?' + urlencode({**params, 'api_key': key}),
                              headers={'Accept': 'application/json', 'X-Request-ID': REQUEST_ID.get() or 'xbyte-demo'})
            try:
                response = build_opener(NoRedirect()).open(request, timeout=deadline.bounded_seconds(45))
            except HTTPError as error:
                response = error
            with response:
                transport_status = response.status
                raw = response.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ConnectorError('invalid_response', 'X-Byte response exceeded the size limit.')
            try:
                payload = json.loads(raw)
            except (ValueError, UnicodeError):
                if transport_status >= 400:
                    code, message = _body_error({}, transport_status)
                    raise ConnectorError(code, message) from None
                raise ConnectorError('invalid_response', 'X-Byte returned invalid JSON.') from None
            if not isinstance(payload, dict):
                raise ConnectorError('invalid_response', 'X-Byte response must be an object.')
            failure = _body_error(payload, transport_status)
            if failure:
                raise ConnectorError(*failure)
            if not 200 <= transport_status < 300:
                raise ConnectorError('provider_error', 'X-Byte returned a non-success transport status.')
            if 'response' not in payload:
                raise ConnectorError('invalid_response', 'X-Byte response envelope is missing.')
            # Vendor request_log can echo api_key inside request_url. Never retain it.
            log = payload.get('request_log', {})
            payload['request_log'] = {k: log[k] for k in ('status', 'process_time') if isinstance(log, dict) and k in log}
            result_code = 'ok'
            return payload
        except (TimeoutError, URLError, OSError) as error:
            result_code = 'provider_timeout' if isinstance(error, TimeoutError) or isinstance(getattr(error, 'reason', None), TimeoutError) else 'provider_unavailable'
            failure = ConnectorError(result_code, 'X-Byte read timed out.' if result_code == 'provider_timeout' else 'X-Byte could not be reached.')
        except ConnectorError as error:
            result_code = error.status
            failure = error
        finally:
            _finish(call_id, result_code)
            emit('xbyte_read', provider='xbyte_foodspark', stage=path.removeprefix('/'),
                 status=result_code, http_status=transport_status, duration_ms=round((time.monotonic()-started)*1000))
        if result_code not in ('provider_timeout', 'provider_unavailable', 'provider_error') or attempt + 1 == attempts:
            raise failure from None
        deadline.check()
        time.sleep(min(.2, deadline.bounded_seconds(.2)))
    raise ConnectorError('provider_error', 'X-Byte read failed.')
