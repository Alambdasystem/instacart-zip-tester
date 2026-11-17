"""Documented FoodSpark Yelp discovery/menu pilot; no ordering or platform matching.

The November 6 guides are the only wired contract. DoorDash and Uber Eats
routes remain unsupported until the vendor supplies their separate contracts.
"""
from collections import OrderedDict
from decimal import Decimal, InvalidOperation
import hashlib
import re
import secrets
import threading
import time
from urllib.parse import urlsplit, urlunsplit

from connector import ConnectorError
from xbyte_transport import enabled, request_json

DISCOVERY_TTL = 300
DISCOVERY_LIMIT = 128
_DISCOVERIES = OrderedDict()
_LOCK = threading.RLock()
_COMMON = {'domain': 'yelp', 'country': 'usa', 'platform': 'web'}


def _enabled(platform):
    if platform != 'yelp':
        raise ConnectorError('unsupported_platform',
                             'Only the documented Yelp demo contract is available; DoorDash and Uber Eats contracts are pending.')
    if not enabled('restaurants_enabled'):
        raise ConnectorError('provider_disabled', 'FoodSpark restaurant pilot is disabled.')


def _utc():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def _text(value, maximum=500):
    return value.strip()[:maximum] if isinstance(value, str) and value.strip() else None


def _url(value, kind='biz'):
    """Return only canonical HTTPS Yelp URLs, never arbitrary vendor fetch targets."""
    if not isinstance(value, str) or len(value) > 1500 or re.search(r'[\x00-\x20\\]', value):
        return None
    try:
        u = urlsplit(value)
        if (u.scheme != 'https' or u.hostname not in ('yelp.com', 'www.yelp.com')
                or u.username or u.password or u.port not in (None, 443)):
            return None
        suffix = r'(?:/item/[A-Za-z0-9_-]+)?' if kind == 'menu' else ''
        if not re.fullmatch(r'/' + kind + r'/[A-Za-z0-9_-]+' + suffix + r'/?', u.path):
            return None
        return urlunsplit(('https', 'www.yelp.com', u.path.rstrip('/'), '', ''))
    except ValueError:
        return None


def _results(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get('response'), dict):
        raise ConnectorError('invalid_response', 'FoodSpark restaurant response has an unrecognized envelope.')
    # request_json validates both transport and body errors before this point.
    if 'results' not in payload['response']:
        raise ConnectorError('invalid_response', 'FoodSpark restaurant response is missing results.')
    return payload['response']['results']


def _prune(now):
    for key, value in list(_DISCOVERIES.items()):
        if now >= value['expires']:
            _DISCOVERIES.pop(key, None)
    while len(_DISCOVERIES) > DISCOVERY_LIMIT:
        _DISCOVERIES.popitem(last=False)


def discovery(zip_code, query, platform='yelp', page_no=0):
    _enabled(platform)
    if (not isinstance(zip_code, str) or not re.fullmatch(r'\d{5}', zip_code)
            or not isinstance(query, str) or not query.strip() or len(query) > 120
            or type(page_no) is not int or page_no < 0 or page_no > 1000 or page_no % 10):
        raise ConnectorError('invalid_request', 'Use a five-digit ZIP, a keyword of 1–120 characters, and page_no 0, 10, 20, ... up to 1000.')
    try:
        payload = request_json('/restaurant_listing', {
            **_COMMON, 'endpoint_type': 'dl295_restaurant_listing_by_keyword_zip_page',
            'search_keyword': query.strip(), 'zip-code': zip_code, 'page_no': page_no,
        })
    except ConnectorError as error:
        if error.status != 'no_match':
            raise
        payload = {'response': {'results': []}}
    rows = _results(payload)
    if not isinstance(rows, list) or len(rows) > 500:
        raise ConnectorError('invalid_response', 'FoodSpark restaurant listing did not contain a bounded results list.')
    retrieved_at = _utc()
    found = OrderedDict()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name, url = _text(row.get('restaurant_name')), _url(row.get('restaurant_url'))
        if not name or not url:
            continue
        address = _text(row.get('address'))
        found[url] = {
            'id': hashlib.sha256((platform + '|' + url).encode()).hexdigest()[:24],
            'name': name, 'restaurant_name': name, 'restaurant_url': url,
            'platform': platform, 'provider': 'foodspark_yelp', 'source': 'foodspark',
            'location': {'address': address, 'requested_zip': zip_code, 'verified': False},
            'has_menu': row.get('has_menu') if type(row.get('has_menu')) is bool else None,
            'captured_at': None, 'retrieved_at': retrieved_at, 'freshness': 'unknown',
            'order_url': None,
        }
    if rows and not found:
        raise ConnectorError('invalid_response', 'FoodSpark returned restaurants without usable names and Yelp URLs.')
    discovery_id = secrets.token_urlsafe(24)
    now = time.monotonic()
    with _LOCK:
        _prune(now)
        _DISCOVERIES[discovery_id] = {
            'expires': now + DISCOVERY_TTL, 'platform': platform, 'items': found,
            'requested_zip': zip_code, 'query': query.strip(),
        }
        _prune(now)
    return {
        'status': 'ok' if found else 'no_match', 'provider': 'foodspark_yelp',
        'source': 'foodspark', 'platform': platform, 'items': list(found.values()),
        'discovery_id': discovery_id, 'expires_in': DISCOVERY_TTL,
        'requested_zip': zip_code, 'query': query.strip(), 'page_no': page_no,
        'next_page_no': page_no + 10 if found and page_no < 1000 else None,
        'has_more': None if found else False, 'coverage_complete': False,
        'warnings': [
            'Discovery keyword results do not establish a menu-item match, price, or delivery coverage.',
            'Vendor capture time and restaurant-stock freshness are not supplied.',
            'The vendor guide conflicts on zip-code versus zipcode; this adapter uses its parameter table and curl example.',
        ],
    }


def _money(value):
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    match = re.fullmatch(r'\$?([0-9]+(?:\.[0-9]{1,2})?)', text)
    if not match:
        return None
    try:
        amount = Decimal(match[1])
        return amount if amount.is_finite() and amount <= Decimal('1000000') else None
    except InvalidOperation:
        return None


def _price(value):
    text = _text(value, 200)
    amount = _money(text)
    if amount is not None:
        return amount, 'exact'
    match = re.fullmatch(r'(?:starting\s+at|from)\s*\$([0-9]+(?:\.[0-9]{1,2})?)', text or '', re.I)
    if match:
        amount = _money(match[1])
        return (amount, 'starting_at') if amount is not None else (None, 'unknown')
    match = re.fullmatch(r'\$([0-9]+(?:\.[0-9]{1,2})?)\+', text or '')
    if match:
        amount = _money(match[1])
        return (amount, 'starting_at') if amount is not None else (None, 'unknown')
    return None, 'unknown'


def _number(amount):
    return float(amount.quantize(Decimal('0.01'))) if amount is not None else None


def _tokens(value):
    def singular(word):
        return word[:-1] if len(word) > 3 and word.endswith('s') and not word.endswith('ss') else word
    return {singular(x) for x in re.findall(r'[a-z0-9]+', value.casefold())}


def _matches(term, item):
    tokens = _tokens(term)
    return bool(tokens) and tokens <= _tokens(item['name'] + ' ' + (item['description'] or ''))


def _component_matches(term, name):
    """Description mentions establish relevance, not included order components."""
    tokens = _tokens(term)
    return bool(tokens) and tokens <= _tokens(name)


def _normalize_item(row, category, restaurant, retrieved_at, ordinal):
    name = _text(row.get('product_name'))
    if not name:
        return None
    amount, price_type = _price(row.get('product_price'))
    menu_url = _url(row.get('product_url'), 'menu')
    if menu_url and urlsplit(menu_url).path.split('/')[2] != urlsplit(restaurant['restaurant_url']).path.split('/')[2]:
        menu_url = None
    modifiers = None
    if isinstance(row.get('required_modifiers'), list):
        modifiers = []
        for modifier in row['required_modifiers'][:50]:
            if not isinstance(modifier, dict) or not _text(modifier.get('name')):
                continue
            value = _money(modifier.get('price'))
            modifiers.append({'name': _text(modifier['name']), 'price': _number(value),
                              'listed_price_text': str(modifier.get('price'))[:200] if modifier.get('price') is not None else None,
                              'currency': 'USD' if value is not None else None, 'required': True})
    complete = (row.get('modifiers_complete') is True and modifiers is not None
                and len(modifiers) == len(row['required_modifiers']))
    semantics = row.get('modifier_pricing_semantics')
    if semantics not in ('additive', 'one_of'):
        semantics = 'unknown'
    semantics_verified = row.get('modifier_pricing_semantics_verified') is True and semantics != 'unknown'
    # Request time has no timezone in the guide; it is not source capture time.
    return {
        'id': hashlib.sha256((restaurant['restaurant_url'] + '|' + str(ordinal) + '|' + name).encode()).hexdigest()[:24],
        'name': name, 'menu_item': name, 'description': _text(row.get('product_description'), 1500),
        'category': _text(category), 'restaurant_name': restaurant['name'],
        'restaurant_url': restaurant['restaurant_url'], 'location': restaurant['location'],
        'provider': 'foodspark_yelp', 'source': 'foodspark', 'platform': 'yelp',
        'price': _number(amount), 'currency': 'USD' if amount is not None else None,
        'listed_price_text': _text(row.get('product_price'), 200), 'price_type': price_type,
        'price_source': 'yelp_menu', 'required_modifiers': modifiers,
        'modifiers_complete': complete, 'modifier_pricing_semantics': semantics,
        'modifier_pricing_semantics_verified': semantics_verified,
        'menu_url': menu_url, 'order_url': None,
        'captured_at': None, 'retrieved_at': retrieved_at, 'freshness': 'unknown',
        'availability': None, 'cart_transfer_supported': False,
    }


def _assessment(items, budget, terms, budget_operator='lt'):
    """Evaluate a specific listed selection; never claim a full-order budget match."""
    selected = []
    unmatched = []
    if terms:
        for term in terms:
            candidates = [item for item in items if _component_matches(term, item['name'])]
            known = [item for item in candidates if item['price'] is not None]
            choice = min(known, key=lambda item: Decimal(str(item['price']))) if known else (candidates[0] if candidates else None)
            if choice is None:
                unmatched.append(term)
            elif choice['id'] not in {item['id'] for item in selected}:
                selected.append(choice)
    else:
        selected = items
    all_priced = bool(selected) and not unmatched and all(item['price'] is not None for item in selected)
    base = sum((Decimal(str(item['price'])) for item in selected if item['price'] is not None), Decimal('0')) if all_priced else None
    exact = all_priced and all(item['price_type'] == 'exact' for item in selected)
    required = [modifier for item in selected for modifier in (item['required_modifiers'] or [])]
    modifier_unknown = (any(not item['modifiers_complete'] for item in selected)
                        or any(modifier['price'] is None for modifier in required))
    semantics_unknown = any(item['required_modifiers'] and not (
        item['modifier_pricing_semantics'] == 'additive' and item['modifier_pricing_semantics_verified'])
        for item in selected)
    # A flat modifier list may contain alternatives. No sum is established until
    # the source explicitly confirms that every listed charge is additive.
    known_modifier_total = (sum((Decimal(str(modifier['price'])) for modifier in required), Decimal('0'))
                            if selected and not modifier_unknown and not semantics_unknown else None)
    overlaps = []
    for owner in selected:
        for modifier in owner['required_modifiers'] or []:
            for term in terms:
                others = [item['id'] for item in selected if item['id'] != owner['id']
                          and _component_matches(term, item['name'])]
                if others and _component_matches(term, modifier['name']):
                    overlaps.append({'modifier_item_id': owner['id'], 'modifier_name': modifier['name'],
                                     'requested_term': term, 'separate_item_ids': others})
    minimum = base + known_modifier_total if base is not None and known_modifier_total is not None and not overlaps else None
    reasons = ['taxes_and_fees_unknown']
    if unmatched:
        reasons.append('requested_items_not_matched')
    if not all_priced:
        reasons.append('missing_listed_price')
    if all_priced and not exact:
        reasons.append('starting_at_price')
    if modifier_unknown:
        reasons.append('required_modifiers_unknown')
    if semantics_unknown:
        reasons.append('required_modifier_selection_unverified')
    if overlaps:
        reasons.append('required_modifier_component_overlap')
    within = lambda amount: amount < budget if budget_operator == 'lt' else amount <= budget
    over = minimum is not None and not within(minimum)
    return {
        'budget': _number(budget), 'currency': 'USD', 'budget_operator': budget_operator,
        'budget_limit_type': 'exclusive' if budget_operator == 'lt' else 'maximum',
        'status': 'over_budget' if over else 'uncertain',
        'selected_item_ids': [item['id'] for item in selected], 'unmatched_terms': unmatched,
        'listed_subtotal': _number(base) if exact else None,
        'minimum_listed_subtotal': _number(base),
        'known_required_modifier_total': _number(known_modifier_total),
        'minimum_subtotal_with_known_required_modifiers': _number(minimum),
        'minimum_subtotal_established': minimum is not None,
        'modifier_component_overlaps': overlaps,
        'listed_subtotal_within_budget': within(base) if exact else (False if base is not None and not within(base) else None),
        'order_within_budget': False if over else None,
        'reason_codes': reasons,
        'selection_method': 'lowest_listed_base_price_per_requested_term' if terms else 'single_menu_item',
        'selection_is_optimization': False,
        'scope': 'Listed menu prices and explicitly supplied required modifiers only; taxes, fees, availability and final checkout price remain unverified.',
    }


def menu(discovery_id, restaurant_url, platform, terms=None, budget=None, budget_operator='lt'):
    _enabled(platform)
    url = _url(restaurant_url)
    if not isinstance(discovery_id, str) or not url:
        raise ConnectorError('invalid_request', 'Pass a discovery_id and its returned HTTPS Yelp restaurant_url.')
    if terms is not None and (not isinstance(terms, list) or not 1 <= len(terms) <= 5
                              or any(not isinstance(term, str) or not term.strip() or len(term) > 120 for term in terms)):
        raise ConnectorError('invalid_request', 'terms must contain 1–5 nonempty item keywords up to 120 characters each.')
    terms = list(dict.fromkeys(term.strip() for term in (terms or [])))
    if budget_operator not in ('lt', 'lte'):
        raise ConnectorError('invalid_request', 'budget_operator must be lt (under) or lte (at most).')
    limit = _money(budget) if budget is not None else None
    if budget is not None and (limit is None or limit <= 0 or limit > Decimal('10000')):
        raise ConnectorError('invalid_request', 'budget must be a positive USD amount with at most two decimal places, up to 10000.')
    with _LOCK:
        _prune(time.monotonic())
        state = _DISCOVERIES.get(discovery_id)
        if not state:
            raise ConnectorError('discovery_expired', 'Repeat restaurant discovery before fetching this menu.')
        if state['platform'] != platform or url not in state['items']:
            raise ConnectorError('discovery_mismatch', 'Use a restaurant URL and platform returned by this discovery.')
        restaurant = dict(state['items'][url])
    if restaurant['has_menu'] is False:
        raise ConnectorError('menu_unavailable', 'Discovery reports no menu for this restaurant.')
    try:
        payload = request_json('/restaurant_menu', {
            **_COMMON, 'endpoint_type': 'dl295_restaurant_menu_by_restaurant_url', 'restaurant_url': url,
        })
    except ConnectorError as error:
        if error.status != 'no_match':
            raise
        payload = {'response': {'results': {}}}
    result = _results(payload)
    if not isinstance(result, dict):
        raise ConnectorError('invalid_response', 'FoodSpark menu did not contain a restaurant object.')
    if result and (_url(result.get('restaurant_url')) != url or not _text(result.get('restaurant_name'))):
        raise ConnectorError('identity_mismatch', 'FoodSpark returned a menu for a different or unidentified restaurant.')
    categories = result.get('categories', [])
    if not isinstance(categories, list) or len(categories) > 100:
        raise ConnectorError('invalid_response', 'FoodSpark menu categories did not match the documented contract.')
    restaurant['name'] = _text(result.get('restaurant_name')) or restaurant['name']
    restaurant['restaurant_name'] = restaurant['name']
    restaurant['location'] = {**restaurant['location'], 'address': _text(result.get('address')) or restaurant['location']['address']}
    retrieved_at = _utc()
    items = []
    for category in categories:
        if not isinstance(category, dict) or not isinstance(category.get('products'), list):
            raise ConnectorError('invalid_response', 'FoodSpark menu products did not match the documented contract.')
        for row in category['products']:
            if len(items) >= 1000:
                raise ConnectorError('invalid_response', 'FoodSpark menu exceeded the bounded item limit.')
            if isinstance(row, dict):
                item = _normalize_item(row, category.get('category'), restaurant, retrieved_at, len(items))
                if item is not None:
                    items.append(item)
    if terms:
        items = [item for item in items if any(_matches(term, item) for term in terms)]
    if limit is not None:
        for item in items:
            item['budget_assessment'] = _assessment([item], limit, [], budget_operator)
    return {
        'status': 'ok' if items else 'no_match', 'provider': 'foodspark_yelp', 'source': 'foodspark',
        'platform': platform, 'restaurant': restaurant, 'items': items,
        'budget_operator': budget_operator,
        'budget_assessment': _assessment(items, limit, terms, budget_operator) if limit is not None and terms else None,
        'cross_platform_matching_supported': False, 'cart_transfer_supported': False,
        'captured_at': None, 'retrieved_at': retrieved_at, 'freshness': 'unknown',
        'warnings': ['Menu prices do not establish availability or a final order total.',
                     'Required modifiers, taxes and fees remain unknown unless explicitly supplied.'],
    }
