"""Bounded public-browser connector. No private API, login, or challenge bypass."""
import hashlib, re, time
from collections import defaultdict, deque
from urllib.parse import quote, urlparse, parse_qs

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
        source_path = card.get('url') or ''
        product_url = 'https://www.instacart.com'+source_path if source_path.startswith('/products/') else None
        source_store = parse_qs(urlparse(source_path).query).get('retailerSlug', [retailer])[0]
        key = (name, text, source_store)
        if key in seen:
            continue
        seen.add(key)
        lines = text.splitlines()
        size = next((line.strip() for line in lines if re.fullmatch(r'\d+(?:\.\d+)?\s*(?:x\s*\d+(?:\.\d+)?\s*)?(?:ct|oz|fl oz|gal|lb|g|kg|L|ml|doz|qt)', line.strip(), re.I)), None)
        image_url=card.get('image_url')
        parsed_image=urlparse(image_url or '')
        if parsed_image.scheme!='https' or not (parsed_image.hostname and (parsed_image.hostname.endswith('.instacart.com') or parsed_image.hostname.endswith('.cloudfront.net') or parsed_image.hostname=='instacart.com')):image_url=None
        price_value = float(price.group(1).replace(',', ''))
        result.append({'id': hashlib.sha256((source_store+'|'+name+'|'+str(size)+'|'+str(price_value)).encode()).hexdigest()[:24], 'name': name, 'size': size, 'price': price_value, 'currency': 'USD', 'retailer': source_store, 'query': query, 'observed_zip': observed_zip, 'source': 'instacart_public_browser', 'image_url':image_url, 'url': product_url or 'https://www.instacart.com/store/'+(retailer+'/' if retailer else '')+'s?k='+quote(query), 'availability': 'Displayed; not independently verified'})
    return balanced_items(result)

def balanced_items(items, per_retailer=8, maximum=80):
    """Round-robin visible retailer groups; a large first shelf cannot starve others."""
    groups=defaultdict(deque)
    for item in items:
        if len(groups[item['retailer']]) < per_retailer:
            groups[item['retailer']].append(item)
    output=[]
    while groups and len(output)<maximum:
        for retailer in list(groups):
            output.append(groups[retailer].popleft())
            if not groups[retailer]: del groups[retailer]
            if len(output)==maximum:break
    return output


SNAPSHOT_JS = """() => {
 const root=document.querySelector('main, [role="main"]');
 const retailers=[...(root?.querySelectorAll('[role="group"][aria-label="retailer"] a[href]')||[])].map(a=>({name:a.innerText.trim(),url:a.getAttribute('href')}));
 const groups=[...(root?.querySelectorAll('[role="group"]')||[])];
 let cards=groups.filter(e=>e.querySelector('h3')&&e.innerText.includes('Current price:'));
 if(!cards.length) cards=[...(root?.querySelectorAll('[role="button"]')||[])].filter(e=>e.querySelector('h3')&&e.innerText.includes('Current price:'));
 return {retailers,cards:cards.map(e=>{
 const a=e.matches('a[data-item-card-button]')?e:e.querySelector('a[data-item-card-button]');
 return {name:e.querySelector('h3').innerText,text:e.innerText,image_url:e.querySelector('img')?.currentSrc||e.querySelector('img')?.getAttribute('src'),url:a?.getAttribute('href')};
 })};
}"""


def observed_location(page):
    """Only the address control is authoritative, never an arbitrary five-digit header string."""
    try:
        control=page.get_by_role('button',name=re.compile(r'^current address:',re.I))
        if not control.count():return None
        label=control.first.get_attribute('aria-label',timeout=1200) or control.first.inner_text(timeout=1200)
        match=re.search(r'\b[0-9]{5}\b',label or '')
        return match.group(0) if match else None
    except Exception:
        return None  # Hydration may detach the control between count and read.


def establish_location(page, requested_zip, allow_unverified):
    try:body=page.locator('body').inner_text(timeout=2500)
    except Exception:body=''
    if re.search(r'verify you are human|checking your browser|unusual traffic|automated queries|access denied',body,re.I):
        raise ConnectorError('provider_blocked','Instacart is screening this browser. No bypass was attempted.')
    control=page.get_by_role('button',name=re.compile(r'^current address:',re.I))
    try:control.first.wait_for(state='visible',timeout=10000)
    except Exception:pass
    observed=observed_location(page)
    if observed!=requested_zip and control.count() and not page.get_by_role('dialog').count():
        field=page.get_by_role('textbox',name='Enter your address',exact=True)
        try:
            control.first.click(timeout=3000)
            field.wait_for(state='visible',timeout=4000)
            field.fill(requested_zip)
            option=page.get_by_role('button',name=re.compile('^'+requested_zip+r'\b'))
            option.first.wait_for(state='visible',timeout=5000)
            option.first.click()
            # A selected suggestion is not proof it was accepted. Read the address control again.
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                observed=observed_location(page)
                if observed==requested_zip:break
                page.wait_for_timeout(400)
        except Exception:pass
    observed=observed_location(page)
    verified=observed==requested_zip
    if not verified and not allow_unverified:
        raise ConnectorError('location_required','Instacart did not confirm the requested ZIP in its active address control. A delivery address or authorized session may be required.')
    if not verified:
        dialog=page.get_by_role('dialog',name='Choose address',exact=True)
        if dialog.count():
            close=dialog.get_by_role('button',name='Close',exact=True)
            if close.count():
                try:close.click(timeout=3000)
                except Exception:pass
    return observed,verified


def wait_for_catalog(page, retailer='', timeout=22, clock=time.monotonic):
    """Wait beyond first paint and require a stable, nonempty retailer/card signature."""
    start=clock();last=None;stable=0;state={'cards':[],'retailers':[]}
    while clock()-start<timeout:
        try:body=page.locator('body').inner_text(timeout=2500)
        except Exception:
            page.wait_for_timeout(750);continue
        if re.search(r'verify you are human|checking your browser|unusual traffic|automated queries|access denied',body,re.I):
            raise ConnectorError('provider_blocked','Instacart is screening this browser. No bypass was attempted.')
        dialogs=page.get_by_role('dialog')
        if dialogs.count():
            text=dialogs.first.inner_text(timeout=2500)
            if re.search(r'Enter your email|Log in|Continue with',text,re.I):
                raise ConnectorError('login_required','Instacart requires interactive login.')
            raise ConnectorError('location_required','Instacart has an active address form. Confirm location before searching.')
        state=page.evaluate(SNAPSHOT_JS)
        signature=tuple((c.get('url'),c.get('name'),c.get('text')) for c in state['cards'])+tuple((r['url'],r['name']) for r in state['retailers'])
        stable=stable+1 if signature and signature==last else 0
        last=signature
        # Multi-retailer discovery gets a longer minimum window, including when only one store exists.
        minimum=4 if retailer else 8
        if state['cards'] and clock()-start>=minimum and stable>=4:
            return state,True
        page.wait_for_timeout(750)
    return state,False


def discovered_retailers(state, items):
    counts=defaultdict(int)
    for item in items:counts[item['retailer']]+=1
    result={}
    for entry in state['retailers']:
        match=re.match(r'^/store/([a-z0-9-]+)/s(?:\?|$)',entry['url'] or '')
        if match:
            slug=match.group(1)
            result[slug]={'id':slug,'name':entry['name'] or slug,'products_returned':counts[slug],'branch_verified':False}
    for slug,count in counts.items():
        if slug:result.setdefault(slug,{'id':slug,'name':slug,'products_returned':count,'branch_verified':False})
    return list(result.values())


def search_browser(zip_code, queries, retailer='', allow_unverified=False):
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as BrowserTimeout
    except ImportError:
        raise ConnectorError('connector_unavailable','Browser dependencies are not installed on this instance.')
    items=[];warnings=[];locations=[];diagnostics=[];retailers={}
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--disable-dev-shm-usage'])
        context=browser.new_context(locale='en-US');page=context.new_page();page.set_default_timeout(5000)
        page.route('**/*',lambda route:route.abort() if route.request.resource_type in ('image','media','font') else route.continue_())
        stage='opening provider search'
        try:
            for query in queries:
                url='https://www.instacart.com/store/'+(retailer+'/' if retailer else '')+'s?k='+quote(query)
                page.goto(url,wait_until='domcontentloaded',timeout=35000)
                stage='confirming address control'
                observed,verified=establish_location(page,zip_code,allow_unverified)
                stage='waiting for retailer shelves to stabilize'
                state,settled=wait_for_catalog(page,retailer)
                # Recheck after hydration/search; never carry verification from a previous query.
                observed=observed_location(page);verified=observed==zip_code
                if not verified and not allow_unverified:
                    raise ConnectorError('location_required','Active provider ZIP changed or could not be confirmed after search.')
                normalized=normalize(state['cards'],query,retailer,observed)
                # Explicit retailer requests must not silently return another retailer.
                if retailer:normalized=[item for item in normalized if item['retailer']==retailer]
                items.extend(normalized);locations.append((observed,verified))
                for r in discovered_retailers(state,normalized):retailers[r['id']]=r
                diagnostics.append({'query':query,'cards_seen':len(state['cards']),'products_returned':len(normalized),'retailers_with_products':len({i['retailer'] for i in normalized}),'settled':settled,'page_url':page.url,'observed_zip':observed})
                if not settled:warnings.append('Visible catalog did not fully stabilize before the time limit; coverage may be partial.')
                if not verified:warnings.append('Requested ZIP '+zip_code+' is unverified; results belong to the provider context, not confirmed neighborhood inventory.')
            if not items:raise ConnectorError('no_results','No readable products matched the selected retailer. This is not proof of no stock.')
            for r in retailers.values():r['products_returned']=sum(i['retailer']==r['id'] for i in items)
            observed_values={o for o,v in locations}
            return {'items':items,'location':{'requested_zip':zip_code,'observed_zip':next(iter(observed_values)) if len(observed_values)==1 else None,'verified':all(v for o,v in locations),'verification_method':'active_address_control','verification_scope':'zip_only','delivery_serviceability_verified':False,'store_branch_verified':False},'retailers':list(retailers.values()),'diagnostics':diagnostics,'warnings':list(dict.fromkeys(warnings))}
        except BrowserTimeout:
            raise ConnectorError('provider_timeout','Instacart timed out while '+stage+'.')
        finally:context.close();browser.close()
