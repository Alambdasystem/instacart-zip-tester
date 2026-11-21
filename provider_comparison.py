"""Active browser comparison with explicit rejection of retired providers."""
import contextvars
import os
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from connector import ConnectorError, matches_query
from deadline import check, DeadlineExceeded
from provider_registry import CAPABILITIES, COMPARISON_PROVIDERS, RETIRED_PROVIDERS, routing_status, source_provider
from provider_session import status as session_status
from search_service import SearchService
from telemetry import emit


class Comparison:
    def __init__(self, catalog, instacart_cache, browser_lock, fetch_instacart):
        self.catalog = catalog
        self.icache = instacart_cache.attach(catalog, 'instacart')
        self.browser_lock = browser_lock
        self.fetch_instacart = fetch_instacart
        with catalog.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS provider_activity (id TEXT PRIMARY KEY, at REAL, provider TEXT, zip TEXT, outcome TEXT, http_status INTEGER, item_count INTEGER, cache_hit INTEGER)')

    @staticmethod
    def require_supported(provider):
        if provider in RETIRED_PROVIDERS:
            raise ConnectorError('provider_removed', 'This provider has been removed; no upstream request was sent.')
        if provider not in COMPARISON_PROVIDERS:
            raise ConnectorError('invalid_provider', 'Use an active comparison provider or the isolated provider-test route.')

    def activity(self, provider, z, outcome, items, status=None, cache_hit=False):
        emit('provider_result', provider=provider, status=outcome, item_count=len(items), http_status=status)
        with self.catalog.db() as db:
            db.execute('INSERT INTO provider_activity VALUES (?,?,?,?,?,?,?,?)',
                       (uuid.uuid4().hex, time.time(), provider, z, outcome, status, len(items), int(cache_hit)))
            db.execute('DELETE FROM provider_activity WHERE id NOT IN (SELECT id FROM provider_activity ORDER BY at DESC LIMIT 1000)')

    def status(self):
        with self.catalog.db() as db:
            rows = db.execute("SELECT at,provider,zip,outcome,http_status,item_count,cache_hit FROM provider_activity WHERE provider='instacart' ORDER BY at DESC LIMIT 50").fetchall()
        return {
            'instacart_session': session_status(), 'capabilities': CAPABILITIES,
            'routing': routing_status(), 'comparison_providers': list(COMPARISON_PROVIDERS),
            'retired_providers': sorted(RETIRED_PROVIDERS),
            'storage': {'engine': 'sqlite', 'persistent_mount_configured': bool(os.getenv('RENDER_DISK_MOUNT_PATH'))},
            'activity': [dict(zip(('at', 'provider', 'zip', 'outcome', 'http_status', 'item_count', 'cache_hit'), row)) for row in rows],
        }

    def one(self, provider, z, queries, store, allow, mode):
        self.require_supported(provider)
        start = time.monotonic()
        items, warnings, cache_hits, diagnostics = [], [], [], []
        location = {'requested_zip': z, 'verified': False}
        found_retailers = {}
        error, upstream, status = None, None, 'ok'
        try:
            if mode == 'catalog':
                items = self.catalog.search(queries, z, store or None, provider=provider)
                if not allow:
                    items = [item for item in items if item.get('location_verified') and item.get('observed_zip') == z]
                status = 'cached' if items else 'not_cached'
                warnings = ['Stored snapshots only; no upstream request. Compare freshness and location before use.']
                cache_hits = [bool(items) and all(item.get('price_fresh') for item in items)]
                observed = {item.get('observed_zip') for item in items}
                location = {'requested_zip': z, 'observed_zip': next(iter(observed)) if len(observed) == 1 else None,
                            'verified': bool(items) and all(item.get('location_verified') for item in items)}
            else:
                for query in queries:
                    check()

                    def fetch(zip_code, terms, retailer, flag):
                        if os.getenv('CONNECTOR_ENABLED', 'false').lower() != 'true':
                            raise ConnectorError('not_configured', 'Instacart browser connector is disabled.')
                        value = self.fetch_instacart(zip_code, terms, retailer, flag)
                        return {**value, 'provider': 'instacart', 'source_provider': 'instacart_browser',
                                'items': [{**item, 'provider': 'instacart', 'source_provider': 'instacart_browser'} for item in value['items']]}

                    entries, hits, fetched, missing = SearchService(self.catalog, self.icache, self.browser_lock, fetch, provider).resolve(z, [query], store, allow, mode)
                    if not entries:
                        continue
                    payload, meta = entries[0]
                    cache_hits.append(bool(hits))
                    location = payload['location']
                    warnings.extend(payload.get('warnings', []))
                    if payload.get('error'):
                        error = payload['error']
                        upstream = error.get('http_status')
                    found_retailers.update({retailer['id']: retailer for retailer in payload.get('retailers', [])})
                    diagnostics.extend(payload.get('diagnostics', []))
                    items.extend({**item, 'provider': provider, 'cache_source': meta.get('source', 'live'),
                                  'age_seconds': meta['age_seconds'], 'price_expires_in': meta['expires_in'],
                                  'price_fresh': meta['expires_in'] > 0, 'retrieved_at': payload.get('retrieved_at'),
                                  'location_verified': payload['location']['verified']} for item in payload['items'])
                status = ('partial' if items else 'error') if error else ('ok' if items else 'not_cached' if mode == 'cache_only' else 'empty')
        except ConnectorError as exc:
            status = 'partial' if items else 'error'
            upstream = getattr(exc, 'http_status', None)
            error = {'code': exc.status, 'message': exc.message, 'http_status': upstream}
        except DeadlineExceeded:
            status = 'partial' if items else 'error'
            error = {'code': 'request_budget_exceeded', 'message': 'Shared search deadline exhausted. Retry missing queries separately.', 'http_status': None}
        except Exception:
            status = 'partial' if items else 'error'
            error = {'code': 'provider_error', 'message': 'Provider lookup failed; no sample results substituted.', 'http_status': None}
        items = list({item['id']: item for item in items if any(matches_query(item.get('name', ''), query) for query in queries)}.values())
        if not items and not error:
            status = 'not_cached' if mode in ('catalog', 'cache_only') else 'empty'
        for item in items:
            item['selectable'] = False
            item['source_provider'] = source_provider(item)
        self.activity(provider, z, status, items, upstream, bool(cache_hits) and all(cache_hits))
        return {
            'cache_sources': sorted({item.get('cache_source', 'catalog_discovery') for item in items}),
            'source_providers': sorted({item['source_provider'] for item in items}),
            'coverage_complete': False, 'source': 'catalog_discovery' if mode == 'catalog' else 'cache_or_live',
            'provider': provider, 'status': status, 'error': error, 'items': items, 'location': location,
            'retailers': list(found_retailers.values()), 'diagnostics': diagnostics,
            'warnings': list(dict.fromkeys(warnings)), 'cache_hit': bool(cache_hits) and all(cache_hits),
            'duration_ms': round((time.monotonic() - start) * 1000),
            'metrics': {'products': len(items), 'with_prices': sum(item.get('price') is not None for item in items),
                        'with_images': sum(bool(item.get('image_url')) for item in items),
                        'fresh': sum(item.get('price_fresh', False) for item in items),
                        'retailers': sorted({item.get('retailer', '') for item in items if item.get('retailer')})},
        }

    def compare(self, z, queries, store, allow, mode, providers):
        # Validate the entire request before submitting even one active provider call.
        for provider in providers:
            self.require_supported(provider)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(contextvars.copy_context().run, self.one, provider, z, queries, store, allow, mode) for provider in providers]
            results = [future.result() for future in futures]
        return {'comparison_id': uuid.uuid4().hex, 'zip': z, 'queries': queries, 'mode': mode,
                'compared_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'providers': results,
                'note': 'Observational browser results. Use /api/v1/provider-tests/compare for isolated Xbyte, Northfork and Actowiz tests.'}
