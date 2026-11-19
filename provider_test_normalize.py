"""Pure, conservative normalization for the isolated provider test namespace.

This module makes no requests and does not infer equivalent products, checkout
readiness, physical branches, or delivery serviceability. Provider quantities are
recommendations, not basket quantities. Decimal strings retain the money value;
the numeric price is supplied only for the existing display contract.
"""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import ipaddress
import json
import math
import re
from urllib.parse import parse_qsl, urlsplit


PROVIDERS = {"xbyte_foodspark", "northfork", "actowiz"}
MODES = {"live", "fixture", "historical"}
_UNIT_NAMES = {
    "oz": "oz", "ounce": "oz", "ounces": "oz", "lb": "lb", "lbs": "lb",
    "pound": "lb", "pounds": "lb", "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "ml": "ml", "l": "l", "fl oz": "fl oz", "floz": "fl oz",
    "ct": "count", "count": "count", "each": "each", "ea": "each", "pce": "each",
}
_FIELDS = {
    "northfork": {
        "id", "external_id", "item_id", "upc", "name", "brand", "size",
        "unit_of_size", "quantity", "unit_of_order", "in_stock", "price",
        "currency_code", "image_url", "pdp_url", "unit_price", "price_kind",
        "price_type", "estimated", "price_basis", "timestamp", "captured_at",
    },
    "xbyte_foodspark": {
        "Product ID", "Product Id", "Product Name", "UPC", "Brand", "Unit Size",
        "Price", "Currency", "Availability", "Product URL", "Product Url",
        "Image URL", "Product Image", "Price Per Unit", "Unit Price", "Price Type", "Price Basis",
        "Timestamp", "Captured At", "product_id", "name", "brand", "size",
        "price", "currency", "availability", "url", "image_url", "unit_price",
        "price_kind", "estimated", "price_basis", "timestamp", "captured_at",
    },
    "actowiz": {
        "product_id", "upc", "item_name", "brand", "size", "pack_size", "price",
        "currency", "regular_price", "promotional_price", "price_per_unit",
        "availability", "timestamp", "store_number", "store_zip", "store_name",
        "source", "product_url", "image", "images", "price_kind", "price_type",
        "estimated", "price_basis", "captured_at",
    },
}


def _pick(record, *keys):
    """First present field, preserving explicit null rather than using a fallback."""
    for key in keys:
        if key in record:
            return record[key]
    return None


def _text(value, limit=500):
    if not isinstance(value, str):
        return None
    value = "".join(c for c in value.strip() if c >= " " and c != "\x7f")
    return value[:limit] or None


def _identifier(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return str(value)
    return _text(value, 200)


def _primitive(value):
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value[:240] if isinstance(value, str) else None


def _json_type(value):
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else "unknown"


def _currency(value):
    value = _text(value, 12)
    return value.upper() if value and re.fullmatch(r"[A-Za-z]{3}", value) else None


def _scale(currency):
    # Only USD minor-unit semantics were verified for these US trial contracts.
    # Preserve other stated currencies, but do not guess their vendor scale.
    return 2 if currency == "USD" else None


def _decimal_text(value, scale=None):
    if scale is not None and value.as_tuple().exponent >= -scale:
        return format(value.quantize(Decimal(1).scaleb(-scale)), "f")
    return format(value, "f")


def _money(raw, currency, *, minor=False, dollar_currency=None, kind_hint=None):
    """Return exact Decimal, currency, kind, original units and quality flags."""
    flags = []
    kind = "exact"
    hint = (_text(kind_hint, 50) or "").lower().replace("-", "_").replace(" ", "_")
    if hint in {"estimated", "estimate", "approximate"}:
        kind = "estimated"
    elif hint in {"starting_at", "from", "minimum"}:
        kind = "starting_at"
    elif hint and hint not in {"exact", "current", "regular", "sale"}:
        kind = "unknown"
        flags.append("unrecognized_price_kind")
    units = "minor_currency_units" if minor else "major_currency_units"
    if raw is None:
        return None, currency, "unknown", units, ["price_missing"]
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float, Decimal)):
        return None, currency, "unknown", "unknown", ["price_invalid_type"]
    value = str(raw).strip()
    if len(value) > 100:
        return None, currency, "unknown", "unknown", ["price_malformed"]
    if isinstance(raw, str):
        estimated = re.match(r"^(?:est(?:imated)?\.?|approx(?:imately)?\.?)\s*", value, re.I)
        starting = re.match(r"^(?:starting\s+(?:at|from)|from)\s*", value, re.I)
        if estimated:
            kind, value = "estimated", value[estimated.end():]
        elif starting:
            kind, value = "starting_at", value[starting.end():]
        if re.search(r"\s*\(?estimated\)?$", value, re.I):
            kind, value = "estimated", re.sub(r"\s*\(?estimated\)?$", "", value, flags=re.I)
        if value.endswith("+"):
            kind, value = "starting_at", value[:-1].strip()
        prefix = re.match(r"^([A-Za-z]{3})\s*", value)
        if prefix:
            stated = prefix[1].upper()
            if currency and stated != currency:
                return None, None, "unknown", "formatted_major_currency", ["currency_conflict"]
            currency, value = stated, value[prefix.end():].strip()
            units = "formatted_major_currency"
        if value[:1] in {"$", "£", "€"}:
            symbol = value[0]
            implied = {"£": "GBP", "€": "EUR"}.get(symbol)
            if implied and currency and implied != currency:
                return None, None, "unknown", "formatted_major_currency", ["currency_conflict"]
            currency = currency or implied or dollar_currency
            value, units = value[1:].strip(), "formatted_major_currency"
        if minor and units != "minor_currency_units":
            return None, currency, "unknown", "unknown", ["minor_price_has_currency_format"]
        if not re.fullmatch(r"(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?", value):
            return None, currency, "unknown", "unknown", ["price_malformed"]
        value = value.replace(",", "")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        return None, currency, "unknown", "unknown", ["price_malformed"]
    if not amount.is_finite() or amount < 0 or amount > Decimal("1000000000000"):
        return None, currency, "unknown", "unknown", ["price_out_of_range"]
    if minor:
        if amount != amount.to_integral_value():
            return None, currency, "unknown", units, ["minor_price_not_integer"]
        scale = _scale(currency)
        if scale is None:
            return None, currency, "unknown", units, ["minor_currency_scale_unknown"]
        amount = amount.scaleb(-scale)
    if currency is None:
        flags.append("currency_missing")
    if kind != "exact":
        flags.append("price_" + kind)
    return amount, currency, kind, units, flags


def _safe_url(value):
    value = _text(value, 4096)
    if not value or re.search(r"\s|[\\]", value):
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return None
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            return None
        try:
            if not ipaddress.ip_address(host).is_global:
                return None
        except ValueError:
            if "." not in host or not re.fullmatch(r"[A-Za-z0-9.-]+", host):
                return None
        parsed.port  # Reject malformed port strings without making a request.
        sensitive = {"key", "api_key", "apikey", "token", "access_token", "security_key",
                     "auth", "authorization", "password", "secret", "signature", "sig"}
        for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
            key = key.lower().replace("-", "_")
            if key in sensitive or any(part in key for part in ("token", "signature", "credential", "secret")):
                return None
        if re.search(r"(?:token|api[_-]?key|password|secret)=", parsed.fragment, re.I):
            return None
        return value
    except (ValueError, TypeError):
        return None


def _timestamp(raw):
    if isinstance(raw, datetime):
        value = raw
    elif isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _zip(value):
    value = _identifier(value)
    return value if value and re.fullmatch(r"\d{5}(?:-\d{4})?", value) else None


def _stock(raw):
    if raw is True:
        return "in_stock"
    if raw is False:
        return "out_of_stock"
    if isinstance(raw, str):
        token = re.sub(r"[\s_-]+", "", raw.strip().lower())
        if token in {"true", "instock", "highlyinstock", "available"}:
            return "in_stock"
        if token in {"false", "outofstock", "unavailable", "notavailable", "soldout"}:
            return "out_of_stock"
    return "unknown"


def _size(row, provider):
    raw = _pick(row, "Unit Size", "size") if provider == "xbyte_foodspark" else row.get("size")
    size = _identifier(raw) if not isinstance(raw, float) else str(raw) if math.isfinite(raw) else None
    unit = _text(row.get("unit_of_size"), 40) if provider == "northfork" else None
    if size and unit and not size.lower().endswith(unit.lower()):
        return size + " " + unit
    return size


def _unit_price(raw, currency):
    """Only accept a price with an explicit, recognized denominator."""
    if raw is None:
        return None
    quantity = Decimal(1)
    if isinstance(raw, dict):
        amount_raw = _pick(raw, "amount", "price")
        unit = _text(_pick(raw, "unit", "unit_of_measure"), 40)
        currency = _currency(_pick(raw, "currency", "currency_code")) or currency
        qty = _pick(raw, "quantity", "unit_quantity")
        if qty is not None:
            if isinstance(qty, bool):
                return None
            try:
                quantity = Decimal(str(qty))
            except InvalidOperation:
                return None
    elif isinstance(raw, str):
        match = re.fullmatch(r"\s*(.+?)\s*(?:/|\bper\b)\s*(?:(\d+(?:\.\d+)?)\s*)?([A-Za-z ]+)\s*", raw, re.I)
        if not match:
            return None
        amount_raw, qty, unit = match.groups()
        quantity = Decimal(qty) if qty else Decimal(1)
    else:
        return None
    unit = _UNIT_NAMES.get((unit or "").strip().lower())
    if not unit or not quantity.is_finite() or quantity <= 0 or quantity > 1000000:
        return None
    amount, currency, kind, _, flags = _money(amount_raw, currency)
    if amount is None or kind != "exact" or "currency_conflict" in flags:
        return None
    return {"amount": float(amount), "amount_decimal": _decimal_text(amount, _scale(currency)),
            "currency": currency, "quantity": float(quantity), "unit": unit}


def _rows(provider, payload, request):
    if provider == "northfork":
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict) or not isinstance(data.get("ingredients"), list):
            raise ValueError("invalid_northfork_payload")
        context = data.get("store") if isinstance(data.get("store"), dict) else {}
        for ingredient in data["ingredients"]:
            if not isinstance(ingredient, dict):
                continue
            products = ingredient.get("products")
            if isinstance(products, list):
                for row in products:
                    if isinstance(row, dict):
                        yield row, _text(ingredient.get("name")), context
    elif provider == "xbyte_foodspark":
        queries = payload.get("queries") if isinstance(payload, dict) else None
        if not isinstance(queries, list):
            raise ValueError("invalid_xbyte_foodspark_payload")
        for entry in queries:
            response = entry.get("response") if isinstance(entry, dict) else None
            if not isinstance(response, dict) or not isinstance(response.get("results"), list):
                raise ValueError("invalid_xbyte_foodspark_response")
            if response.get("error"):
                raise ValueError("provider_error_payload")
            context = response.get("store_details")
            context = context if isinstance(context, dict) else {}
            for row in response["results"]:
                if isinstance(row, dict):
                    yield row, _text(entry.get("query")), context
    else:
        rows = payload if isinstance(payload, list) else [payload]
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError("invalid_actowiz_payload")
            status = row.get("status_code")
            if row.get("error") or (isinstance(status, int) and not isinstance(status, bool) and status >= 400):
                raise ValueError("provider_error_payload")
            if any(key in row for key in _FIELDS[provider]):
                query = _text(request.get("query"))
                queries = request.get("queries")
                if query is None and isinstance(queries, list) and len(queries) == 1:
                    query = _text(queries[0])
                yield row, query, {}


def _location(provider, row, context, request, store, retailer, flags):
    requested = _zip(_pick(request, "zip", "requested_zip", "zipcode"))
    requested = requested or _zip(store.get("requested_zip"))
    expected_store = _identifier(_pick(store, "provider_store_id", "identifier", "store_id"))
    if provider != "xbyte_foodspark":
        expected_store = expected_store or _identifier(store.get("id"))
    else:
        expected_store = expected_store or _identifier(store.get("Store Location ID"))
    expected_zip = _zip(_pick(store, "store_zip", "zip", "zipcode"))
    if provider == "northfork":
        observed_store = _identifier(context.get("identifier"))
        observed = _zip(context.get("zip"))
        if observed is None and observed_store and observed_store == expected_store:
            observed = expected_zip  # Selected discovery record, not the user's shopping ZIP.
    elif provider == "xbyte_foodspark":
        observed_store = _identifier(context.get("Store Location ID"))
        observed = _zip(context.get("Delivery Zip Code"))
        expected_zip = requested  # This field is a request echo, not a branch ZIP.
    else:
        observed_store = _identifier(row.get("store_number"))
        observed = _zip(row.get("store_zip"))
    mismatch, acknowledged = False, False
    if expected_store and observed_store:
        if expected_store != observed_store:
            flags.append("store_id_mismatch")
            mismatch = True
        else:
            acknowledged = True
    if expected_zip and observed:
        if expected_zip != observed:
            flags.append("zip_mismatch")
            mismatch = True
        else:
            acknowledged = True
    expected_retailer = _text(_pick(request, "retailer", "brand_slug"))
    if provider == "xbyte_foodspark" and expected_retailer and retailer:
        if expected_retailer.casefold() != retailer.casefold():
            flags.append("retailer_mismatch")
            mismatch = True
    evidence = "mismatch" if mismatch else "request_acknowledged" if acknowledged else "unknown"
    return requested, observed, observed_store, evidence


def normalize(provider, payload, request, store, retrieved_at, mode="live"):
    """Return display-only canonical items without changing any provider state.

    ``store`` is a selected discovery record; address and coordinate fields are
    deliberately ignored. A matching store/ZIP echo acknowledges a request only.
    Unknown stock does not prevent list display. Invalid envelopes raise a
    generic ValueError, with no vendor payload or credentials in the message.
    """
    if provider not in PROVIDERS:
        raise ValueError("unsupported_provider")
    if mode not in MODES:
        raise ValueError("unsupported_source_mode")
    request = request if isinstance(request, dict) else {}
    store = store if isinstance(store, dict) else {}
    retrieved = _timestamp(retrieved_at)
    items = []
    for row, query, context in _rows(provider, payload, request):
        flags = []
        if provider == "northfork":
            name = _text(row.get("name"))
            product_id = _identifier(row.get("external_id")) or _identifier(row.get("id"))
            raw_price, raw_stock = row.get("price"), row.get("in_stock")
            currency = _currency(row.get("currency_code"))
            retailer = (_text(store.get("retailer_key")) or _text(store.get("retailer"))
                        or _text(store.get("name")))
            platform = _text(context.get("provider")) or _text(store.get("provider"))
            product_url, image = row.get("pdp_url"), row.get("image_url")
            raw_unit = row.get("unit_price")
        elif provider == "xbyte_foodspark":
            name = _text(_pick(row, "Product Name", "name"))
            product_id = _identifier(_pick(row, "Product ID", "Product Id", "product_id"))
            raw_price, raw_stock = _pick(row, "Price", "price"), _pick(row, "Availability", "availability")
            currency = _currency(_pick(row, "Currency", "currency"))
            retailer = _text(context.get("Store Slug")) or _text(request.get("retailer"))
            platform = "instacart"  # Scope of the delivered US FoodSpark grocery endpoint.
            product_url = _pick(row, "Product URL", "Product Url", "url")
            image = _pick(row, "Image URL", "Product Image", "image_url")
            raw_unit = _pick(row, "Price Per Unit", "Unit Price", "unit_price")
        else:
            name = _text(row.get("item_name"))
            product_id = _identifier(row.get("product_id"))
            raw_price, raw_stock = row.get("price"), row.get("availability")
            currency = _currency(row.get("currency"))
            retailer = _text(row.get("source")) or "walmart"
            platform = "walmart"  # Only the delivered Walmart product lookup is supported here.
            product_url, image = row.get("product_url"), row.get("image")
            if image is None and isinstance(row.get("images"), list):
                image = next((url for url in row["images"] if _safe_url(url)), None)
            raw_unit = row.get("price_per_unit")
        hint = _pick(row, "price_kind", "price_type", "Price Type")
        if row.get("estimated") is True:
            hint = "estimated"
        amount, currency, kind, units, money_flags = _money(
            raw_price, currency, minor=provider == "northfork",
            dollar_currency="USD" if provider == "xbyte_foodspark" else None,
            kind_hint=hint,
        )
        flags.extend(money_flags)
        price_minor = None
        scale = _scale(currency)
        if amount is not None and scale is not None:
            minor_value = amount.scaleb(scale)
            if minor_value == minor_value.to_integral_value():
                price_minor = int(minor_value)
            else:
                flags.append("fractional_minor_unit")
        unit_price = _unit_price(raw_unit, currency)
        if raw_unit is not None and unit_price is None:
            flags.append("unit_price_ambiguous")
        raw_time = _pick(row, "captured_at", "Captured At", "timestamp", "Timestamp")
        captured = _timestamp(raw_time)
        if captured is None:
            flags.append("source_timestamp_missing" if raw_time is None else "source_timestamp_ambiguous")
        requested, observed, store_id, evidence = _location(provider, row, context, request, store, retailer, flags)
        if mode == "fixture":
            evidence = "unknown"
            flags.append("synthetic_fixture")
        if not name:
            flags.append("name_missing")
        url, image_url = _safe_url(product_url), _safe_url(image)
        if product_url is not None and url is None:
            flags.append("product_url_rejected")
        if image is not None and image_url is None:
            flags.append("image_url_rejected")
        upc_raw = _pick(row, "upc", "UPC")
        upc = _identifier(upc_raw)
        if isinstance(upc_raw, int) and not isinstance(upc_raw, bool):
            flags.append("numeric_upc_may_have_lost_leading_zeros")
        brand = _text(_pick(row, "brand", "Brand"))
        if brand and brand.lower() in {"na", "n/a", "unknown", "null"}:
            brand = None
        size = _size(row, provider)
        provider_quantity = _primitive(row.get("quantity")) if provider == "northfork" else None
        if isinstance(provider_quantity, bool):
            provider_quantity = None
            flags.append("provider_quantity_invalid")
        basis_raw = _text(_pick(row, "price_basis", "Price Basis"), 50)
        basis = "known" if basis_raw and basis_raw.lower() in {
            "item", "per_item", "each", "retailer_direct", "in_store", "online", "pickup", "delivery"
        } else "unspecified"
        identity = [provider, mode, platform, retailer, requested, store_id, product_id, upc, name, size]
        digest = hashlib.sha256(json.dumps(identity, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()[:24]
        items.append({
            "id": "test:" + provider + ":" + digest,
            "provider": provider, "platform": platform, "retailer": retailer, "query": query,
            "provider_product_id": product_id, "upc": upc, "name": name, "brand": brand,
            "size": size, "provider_quantity": provider_quantity,
            "unit_of_order": _text(row.get("unit_of_order"), 40) if provider == "northfork" else None,
            "price": float(amount) if amount is not None else None,
            "price_decimal": _decimal_text(amount, scale) if amount is not None else None,
            "price_minor": price_minor, "currency": currency, "price_kind": kind,
            "price_basis": basis, "unit_price": unit_price,
            "availability": _stock(raw_stock), "availability_raw": _primitive(raw_stock),
            "image_url": image_url, "url": url,
            "requested_zip": requested, "observed_zip": observed, "provider_store_id": store_id,
            "location_evidence": evidence, "captured_at": captured, "retrieved_at": retrieved,
            "quality_flags": list(dict.fromkeys(flags)),
            "raw_field_types": {key: _json_type(value) for key, value in row.items() if key in _FIELDS[provider]},
            "raw_price": _primitive(raw_price), "raw_price_unit": units,
            "source_mode": mode, "live": mode == "live", "selectable": False,
            "cart_transfer_supported": provider == "northfork", "transfer_verified": False,
            "list_display_supported": bool(name),
        })
    return items
