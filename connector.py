"""Bounded public-browser connector. No private API, login, or challenge bypass."""
import hashlib, re, time
from urllib.parse import quote

class ConnectorError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)

def normalize(cards, query, retailer, observed_zip):
    result, seen = [], set()
    for card in cards:
        name = card.get('name', '').strip()
        text = card.get('text', '')
        price = re.search(r'Current price:\s*\$([\d,]+\.\d{2})', text)
        if not name or not price:
            continue
        key = (name, text)
        if key in seen:
            continue
        seen.add(key)
        lines = text.splitlines()
        size = next((line.strip() for line in lines if re.fullmatch(r'\d+(?:\.\d+)?\s*(?:x\s*\d+(?:\.\d+)?\s*)?(?:ct|oz|fl oz|gal|lb|g|kg|L|ml|doz|qt)', line.strip(), re.I)), None)
        price_value = float(price.group(1).replace(',', ''))
        result.append({'id': hashlib.sha256((retailer+'|'+name+'|'+str(size)+'|'+str(price_value)).encode()).hexdigest()[:24], 'name': name, 'size': size, 'price': price_value, 'currency': 'USD', 'retailer': retailer, 'query': query, 'observed_zip': observed_zip, 'source': 'instacart_public_browser', 'url': 'https://www.instacart.com/store/'+(retailer+'/' if retailer else '')+'s?k='+quote(query), 'availability': 'Displayed; not independently verified'})
    return result[:20]

def search_browser(zip_code, queries, retailer='', allow_unverified=False):
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    except ImportError:
        raise ConnectorError('connector_unavailable', 'Browser dependencies are not installed on this instance.')
    items, observed_zip, verified, warnings = [], None, False, []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--disable-dev-shm-usage'])
        context = browser.new_context(locale='en-US')
        page = context.new_page()
        page.set_default_timeout(5000)
        try:
            for index, query in enumerate(queries):
                url = 'https://www.instacart.com/store/'+(retailer+'/' if retailer else '')+'s?k='+quote(query)
                page.goto(url, wait_until='domcontentloaded', timeout=25000)
                deadline = time.monotonic()+15
                while time.monotonic() < deadline:
                    text = page.locator('body').inner_text(timeout=5000)
                    if re.search(r'verify you are human|checking your browser|unusual traffic|automated queries|access denied', text, re.I):
                        raise ConnectorError('provider_blocked', 'Instacart is screening this browser. No bypass was attempted.')
                    if 'Current price:' in text or page.get_by_role('dialog').count():
                        break
                    page.wait_for_timeout(750)
                if index == 0:
                    header = page.locator('header, [role="banner"]').inner_text(timeout=5000)
                    match = re.search(r'\b\d{5}\b', header)
                    observed_zip = match.group(0) if match else None
                    verified = observed_zip == zip_code
                    if not verified and not allow_unverified:
                        # Only interact with the visible address selector; ZIP-only may require a street address.
                        controls = page.get_by_role('button').filter(has_text=re.compile(r'^\d{5}$'))
                        if controls.count() and not page.get_by_role('dialog').count():
                            controls.first.click()
                            field = page.get_by_role('textbox', name='Enter your address', exact=True)
                            if field.count():
                                field.fill(zip_code)
                                option = page.get_by_role('button', name=re.compile('^'+zip_code+r'\b'))
                                try:
                                    option.first.wait_for(state='visible', timeout=5000)
                                    option.first.click()
                                except BrowserTimeout:
                                    pass
                                header = page.locator('header, [role="banner"]').inner_text(timeout=5000)
                                match = re.search(r'\b\d{5}\b', header)
                                observed_zip = match.group(0) if match else observed_zip
                                verified = observed_zip == zip_code
                        if not verified and not allow_unverified:
                            raise ConnectorError('location_required', 'ZIP alone could not establish the requested delivery location. Supply an authorized provider session in a future connector, or explicitly allow unverified-location results.')
                        if not verified:
                            warnings.append('Prices are not verified for requested ZIP '+zip_code+'. Confirm the delivery address on Instacart.')
                if not verified and not allow_unverified:
                    raise ConnectorError('location_required', 'Requested ZIP could not be verified.')
                if not verified and allow_unverified:
                    warnings.append('Location is unverified. These prices may belong to a different ZIP.')
                # Do not scrape through an active sign-in wall or an address form.
                dialogs = page.get_by_role('dialog')
                if dialogs.count():
                    dialog_text = dialogs.first.inner_text(timeout=5000)
                    if re.search(r'Enter your email|Log in|Continue with', dialog_text, re.I):
                        raise ConnectorError('login_required', 'Instacart requires an interactive login in this browser.')
                    if not verified:
                        raise ConnectorError('location_required', 'Instacart requires a street address before continuing this search.')
                cards = page.locator('main [role="group"]').evaluate_all("els => els.filter(e => e.querySelector('h3') && e.innerText.includes('Current price:')).map(e => ({name:e.querySelector('h3').innerText,text:e.innerText}))")
                if not cards:
                    cards = page.locator('main button').evaluate_all("els => els.filter(e => e.querySelector('h3')).map(e => ({name:e.querySelector('h3').innerText,text:e.innerText}))")
                items.extend(normalize(cards, query, retailer, observed_zip))
            if not items:
                raise ConnectorError('no_results', 'No readable product-and-price cards were returned. This is not proof of no stock.')
            return {'items': items, 'location': {'requested_zip': zip_code, 'observed_zip': observed_zip, 'verified': verified}, 'warnings': list(dict.fromkeys(warnings))}
        except BrowserTimeout:
            raise ConnectorError('provider_timeout', 'Instacart did not finish rendering within the connector timeout.')
        finally:
            context.close()
            browser.close()
