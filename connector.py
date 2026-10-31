from deadline import check, bounded_ms, DeadlineExceeded
from page_state import classify_dialog
from telemetry import emit
from location_context import current_location, context_id
"""Bounded public-browser connector. No private API, login, or challenge bypass."""
import hashlib, re, time
from collections import defaultdict, deque
from urllib.parse import quote, urlparse, parse_qs

class ConnectorError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message
        super().__init__(message)

def matches_query(name,query):
    """Conservative lexical match; recommendations aren't exact query coverage."""
    def words(value):
        tokens=re.findall(r'[a-z0-9]+',value.casefold())
        irregular={'tomatoes':'tomato','potatoes':'potato','berries':'berry'}
        return {irregular.get(t,t[:-1] if len(t)>3 and t.endswith('s') else t) for t in tokens if t not in {'and','the','with'}}
    wanted=words(query)
    return not wanted or wanted.issubset(words(name))

def normalize(cards, query, retailer, observed_zip):
    result, seen = [], set()
    for card in cards:
        name = card.get('name', '').strip()
        text = card.get('text', '')
        price = re.search(r'Current price:\s*\$([\d,]+\.\d{2})', text)
        if not name or not price or not matches_query(name,query) or re.search(r'\b(conditioner|shampoo|body wash|soap|lotion|lip balm|detergent|moisturizer)\b',name,re.I):
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
        result.append({'id': hashlib.sha256((source_store+'|'+(source_path.split('?')[0] or name+'|'+str(size))).encode()).hexdigest()[:24], 'name': name, 'size': size, 'price': price_value, 'currency': 'USD', 'retailer': source_store, 'query': query, 'observed_zip': observed_zip, 'source': 'instacart_public_browser', 'image_url':image_url, 'url': product_url or 'https://www.instacart.com/store/'+(retailer+'/' if retailer else '')+'s?k='+quote(query), 'availability': 'Displayed; not independently verified'})
    return balanced_items(result)

def balanced_items(items, per_retailer=None, maximum=500):
    """Round-robin visible retailer groups; a large first shelf cannot starve others."""
    groups=defaultdict(deque)
    for item in items:
        if per_retailer is None or len(groups[item['retailer']]) < per_retailer:
            groups[item['retailer']].append(item)
    output=[]
    while groups and len(output)<maximum:
        for retailer in list(groups):
            output.append(groups[retailer].popleft())
            if not groups[retailer]: del groups[retailer]
            if len(output)==maximum:break
    return output


SNAPSHOT_JS = r"""() => {
 const root=document.querySelector('main, [role="main"]');
 const retailers=[...(root?.querySelectorAll('[role="group"][aria-label="retailer"] a[href]')||[])].map(a=>({name:a.innerText.trim(),url:a.getAttribute('href')}));
 const anchors=[...(root?.querySelectorAll('a[data-item-card-button][href]')||[])];
 let cards=anchors.map(a=>{
 const e=a.closest('[role="group"]')||a;
 const heading=e.querySelector('h3, h2, [role="heading"]');
 return {name:heading?.innerText||e.querySelector('img')?.alt||'',text:e.innerText+'\n'+(a.getAttribute('aria-label')||''),image_url:e.querySelector('img')?.currentSrc||e.querySelector('img')?.getAttribute('src'),url:a.getAttribute('href')};
 });
 if(!cards.length){
 const groups=[...(root?.querySelectorAll('[role="group"], [role="button"]')||[])];
 cards=groups.filter(e=>e.querySelector('h3')&&e.innerText.includes('Current price:')).map(e=>({name:e.querySelector('h3').innerText,text:e.innerText,image_url:e.querySelector('img')?.currentSrc||e.querySelector('img')?.getAttribute('src'),url:e.querySelector('a[data-item-card-button]')?.getAttribute('href')}));
 }
 return {retailers,cards,has_next_page:!!root?.querySelector('button[aria-label="Next page"]')};
}"""


def street_tokens(text):
    text=re.sub(r'^current address:\s*','',text,flags=re.I).split('. Or select')[0]
    return [t for t in re.findall(r'[a-z0-9]+',text.casefold()) if t not in {'road','rd','street','st','avenue','ave','drive','dr','boulevard','blvd'}]

def observed_location(page):
    """Only the address control is authoritative, never an arbitrary five-digit header string."""
    try:
        control=page.get_by_role('button',name=re.compile(r'^current address:',re.I))
        if not control.count():return None
        label=control.first.get_attribute('aria-label',timeout=bounded_ms(1200)) or control.first.inner_text(timeout=bounded_ms(1200))
        match=re.search(r'\b[0-9]{5}\b',label or '')
        if match:return match.group(0)
        confirmation=getattr(page,'_instasearch_address_confirmation',None)
        if isinstance(confirmation,dict) and street_tokens(label or '')[:len(confirmation['tokens'])]==confirmation['tokens']:return confirmation['zip']
        return None
    except Exception:
        return None  # Hydration may detach the control between count and read.


def establish_location(page, requested_zip, allow_unverified):
    try:body=page.locator('body').inner_text(timeout=bounded_ms(2500))
    except Exception:body=''
    if re.search(r'verify you are human|checking your browser|unusual traffic|automated queries|access denied',body,re.I):
        raise ConnectorError('provider_blocked','Instacart is screening this browser. No bypass was attempted.')
    control=page.get_by_role('button',name=re.compile(r'^current address:',re.I))
    try:control.first.wait_for(state='visible',timeout=bounded_ms(10000))
    except Exception:pass
    observed=observed_location(page)
    requested=current_location(requested_zip)
    address=requested.get('address')
    if requested.get('coordinates'):raise ConnectorError('location_unsupported','Instacart browser requires an address; GPS-only selection is not supported by this connector.')
    address_selected=False
    save_clicked=False;save_enabled=False;active_matches=False;suggestion_matches=False
    opened_picker=False
    address_stage='opening_picker';address_failure=None
    field=page.get_by_role('textbox',name='Enter your address',exact=True)
    dialogs=page.get_by_role('dialog')
    existing_picker=False
    if dialogs.count():
        try:
            dialog_kind=classify_dialog(dialogs.first.inner_text(timeout=bounded_ms(2000)))
            if dialog_kind in ('login_required','provider_blocked'):raise ConnectorError(dialog_kind,'Provider requires authorized access before location selection.')
            existing_picker=dialog_kind=='address_dialog' and bool(field.count())
        except ConnectorError:raise
        except Exception:pass
    if (observed!=requested_zip or address) and ((control.count() and not dialogs.count()) or existing_picker):
        try:
            if not existing_picker:control.first.click(timeout=bounded_ms(3000))
            opened_picker=True
            address_stage='address_input'
            field.wait_for(state='visible',timeout=bounded_ms(8000))
            search_text=(', '.join([address['line1'],address['city'],address['region'],requested_zip]) if address else requested_zip)
            field.fill(search_text)
            address_tokens=street_tokens(address['line1']) if address else []
            suggestion_pattern=r'^'+r'\s+'.join(map(re.escape,address_tokens)) if address else '^'+requested_zip+r'\b'
            option=page.get_by_role('button',name=re.compile(suggestion_pattern,re.I))
            address_stage='address_suggestion'
            option.first.wait_for(state='visible',timeout=bounded_ms(15000))
            suggestion_text=option.first.inner_text(timeout=bounded_ms(2000)) if address else ''
            option.first.click()
            if address:
                save=page.get_by_role('button',name=re.compile(r'^Save address$',re.I))
                address_stage='save_address_form'
                save.first.wait_for(state='visible',timeout=bounded_ms(12000))
                if save.count():
                    address_stage='save_address_enabled'
                    save_deadline=time.monotonic()+12
                    while not save.first.is_enabled() and time.monotonic()<save_deadline:page.wait_for_timeout(bounded_ms(400))
                    save_enabled=save.first.is_enabled()
                    if save_enabled:
                        save_clicked=True
                        address_stage='save_address_acknowledgment'
                        save.first.click(timeout=bounded_ms(4000))
                        dialog=page.get_by_role('dialog')
                        if dialog.count():dialog.first.wait_for(state='hidden',timeout=bounded_ms(12000))
                # Saving may unmount/remount the modal before updating the header.
                # Poll both acknowledgment signals, rather than sampling the old ZIP.
                address_stage='active_address_control' if save_clicked else 'save_address_enabled'
                tokens=street_tokens(address['line1'])
                suggestion_matches=bool(re.search(r'\b'+requested_zip+r'\b',suggestion_text))
                ack_deadline=time.monotonic()+20
                while time.monotonic()<ack_deadline:
                    active=(control.first.get_attribute('aria-label',timeout=bounded_ms(1200)) or control.first.inner_text(timeout=bounded_ms(1200)) or '')
                    active_matches=street_tokens(active)[:len(tokens)]==tokens
                    address_selected=save_clicked and active_matches and suggestion_matches and not page.get_by_role('dialog').count()
                    if address_selected:
                        page._instasearch_address_confirmation={'zip':requested_zip,'tokens':tokens}
                        break
                    if not save_clicked:break
                    page.wait_for_timeout(bounded_ms(400))
            # A selected suggestion is not proof it was accepted. Read the address control again.
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                observed=observed_location(page)
                if observed==requested_zip:break
                page.wait_for_timeout(bounded_ms(400))
        except DeadlineExceeded:raise
        except Exception as e:address_failure=type(e).__name__
    observed=observed_location(page)
    verified=observed==requested_zip and (not address or address_selected)
    if address and not address_selected:raise ConnectorError('location_required','Provider did not acknowledge the submitted address at '+address_stage+('. '+address_failure if address_failure else '')+'. save_clicked='+str(save_clicked)+', active_street_match='+str(active_matches)+', suggestion_zip_match='+str(suggestion_matches)+'. Complete the address or authorized session setup.')
    if not verified and not allow_unverified:
        raise ConnectorError('location_required','Instacart did not confirm the requested ZIP in its active address control. A delivery address or authorized session may be required.')
    if not verified:
        # ZIP suggestions may change the dialog title. Only dismiss an optional
        # address picker with its visible close control; never a sign-in wall.
        dialogs=page.get_by_role('dialog')
        if dialogs.count():
            dialog=dialogs.first
            try:text=dialog.inner_text(timeout=bounded_ms(2000))
            except Exception:text=''
            if classify_dialog(text)=='login_required':
                raise ConnectorError('login_required','Instacart requires interactive login.')
            if opened_picker or re.search(r'address|delivery|location',text,re.I):
                close=dialog.get_by_role('button',name=re.compile(r'^close(?:\b|$)',re.I))
                if close.count():
                    try:
                        close.first.click(timeout=bounded_ms(5000))
                        dialog.wait_for(state='hidden',timeout=bounded_ms(5000))
                    except Exception:pass
    return observed,verified


def wait_for_catalog(page, retailer='', timeout=22, clock=time.monotonic):
    """Wait beyond first paint and require a stable, nonempty retailer/card signature."""
    start=clock();last=None;stable=0;state={'cards':[],'retailers':[]};seen_cards={};seen_retailers={};scroll_index=0
    while clock()-start<timeout:
        try:body=page.locator('body').inner_text(timeout=bounded_ms(2500))
        except Exception:
            check();page.wait_for_timeout(bounded_ms(750));continue
        if re.search(r'verify you are human|checking your browser|unusual traffic|automated queries|access denied',body,re.I):
            raise ConnectorError('provider_blocked','Instacart is screening this browser. No bypass was attempted.')
        dialogs=page.get_by_role('dialog')
        if dialogs.count():
            try:text=dialogs.first.inner_text(timeout=bounded_ms(2500))
            except Exception:
                check();page.wait_for_timeout(bounded_ms(750));continue
            if classify_dialog(text)=='login_required':
                raise ConnectorError('login_required','Instacart requires interactive login.')
            raise ConnectorError('location_required','Instacart has an active address form. Confirm location before searching.')
        current=page.evaluate(SNAPSHOT_JS)
        for c in current['cards']:seen_cards[(c.get('url'),c.get('name'))]=c
        for r in current['retailers']:seen_retailers[r.get('url')]=r
        state={**current,'cards':list(seen_cards.values()),'retailers':list(seen_retailers.values())}
        # Public shelves lazy-load on intersection. Visit each discovered shelf,
        # retaining observations in case the page virtualizes previous shelves.
        shelves=page.locator('[role="group"][aria-label="retailer"]')
        if scroll_index<shelves.count():
            try:shelves.nth(scroll_index).scroll_into_view_if_needed(timeout=bounded_ms(2500))
            except Exception:pass
            scroll_index+=1;stable=0
            check();page.wait_for_timeout(bounded_ms(750))
            continue
        signature=tuple((c.get('url'),c.get('name'),c.get('text')) for c in state['cards'])+tuple((r['url'],r['name']) for r in state['retailers'])
        stable=stable+1 if signature and signature==last else 0
        last=signature
        # Multi-retailer discovery gets a longer minimum window, including when only one store exists.
        minimum=4 if retailer else 12
        if state['cards'] and clock()-start>=minimum and stable>=4:
            return state,True
        check();page.wait_for_timeout(bounded_ms(750))
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
    started=time.monotonic()
    emit('search_started',provider='instacart',query_count=len(queries))
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,timeout=bounded_ms(30000),args=['--disable-dev-shm-usage'])
        context=browser.new_context(locale='en-US');page=context.new_page();page.set_default_timeout(5000)
        page.route('**/*',lambda route:route.abort() if route.request.resource_type in ('image','media','font') else route.continue_())
        stage='opening provider search'
        try:
            for query in queries:
                check()
                if items and time.monotonic()-started>90:
                    warnings.append('Request time budget reached; remaining queries were not fetched. Retry missing queries separately.')
                    break
                stage='opening provider search'
                phase_started=time.monotonic()
                emit('search_stage',provider='instacart',stage=stage)
                bootstrap_address=bool(retailer and current_location(zip_code).get('address'))
                url='https://www.instacart.com/store/'+(retailer+'/' if retailer and not bootstrap_address else '')+'s?k='+quote(query)
                page.goto(url,wait_until='commit',timeout=bounded_ms(35000))
                emit('search_stage_complete',provider='instacart',stage=stage,duration_ms=round((time.monotonic()-phase_started)*1000))
                stage='confirming address control'
                phase_started=time.monotonic()
                emit('search_stage',provider='instacart',stage=stage)
                observed,verified=establish_location(page,zip_code,allow_unverified)
                emit('search_stage_complete',provider='instacart',stage=stage,duration_ms=round((time.monotonic()-phase_started)*1000))
                stage='waiting for retailer shelves to stabilize'
                phase_started=time.monotonic()
                emit('search_stage',provider='instacart',stage=stage)
                if bootstrap_address:
                    links=page.locator('a[href]')
                    target=None
                    for n in range(min(links.count(),300)):
                        href=links.nth(n).get_attribute('href') or ''
                        if re.match(r'^/store/'+re.escape(retailer)+r'/s(?:\?|$)',href):target=links.nth(n);break
                    if target is None:raise ConnectorError('retailer_unavailable','Requested retailer navigation is not visible in the confirmed context.')
                    target.click(timeout=bounded_ms(5000))
                    page.wait_for_timeout(bounded_ms(500))
                state,settled=wait_for_catalog(page,retailer)
                emit('search_stage_complete',provider='instacart',stage=stage,duration_ms=round((time.monotonic()-phase_started)*1000))
                # Recheck after hydration/search; never carry verification from a previous query.
                observed=observed_location(page);verified=verified and observed==zip_code
                if not verified and not allow_unverified:
                    raise ConnectorError('location_required','Active provider ZIP changed or could not be confirmed after search.')
                normalized=normalize(state['cards'],query,retailer,observed)
                # Explicit retailer requests must not silently return another retailer.
                if retailer:normalized=[item for item in normalized if item['retailer']==retailer]
                items.extend(normalized);locations.append((observed,verified))
                for r in discovered_retailers(state,normalized):retailers[r['id']]=r
                diagnostics.append({'query':query,'cards_seen':len(state['cards']),'products_returned':len(normalized),'retailers_with_products':len({i['retailer'] for i in normalized}),'settled':settled,'collection_limit':500,'pagination_available':bool(state.get('has_next_page')),'page_url':page.url,'observed_zip':observed})
                if len(normalized)>=500 or state.get('has_next_page'):warnings.append('Results cover the current visible page; additional provider pages may exist. No retailer allowlist is applied.')
                if not settled:warnings.append('Visible catalog did not fully stabilize before the time limit; coverage may be partial.')
                if not verified:warnings.append('Requested ZIP '+zip_code+' is unverified; results belong to the provider context, not confirmed neighborhood inventory.')
            if not items:raise ConnectorError('no_results','No readable products matched the selected retailer. This is not proof of no stock.')
            for r in retailers.values():r['products_returned']=sum(i['retailer']==r['id'] for i in items)
            observed_values={o for o,v in locations}
            return {'items':items,'location':{'requested_zip':zip_code,'observed_zip':next(iter(observed_values)) if len(observed_values)==1 else None,'verified':all(v for o,v in locations),'context_id':context_id(current_location(zip_code)),'address_selected':bool(current_location(zip_code).get('address')) and all(v for o,v in locations),'verification_method':'active_address_control','verification_scope':'zip_only','delivery_serviceability_verified':False,'store_branch_verified':False},'direct_local_supported':False,'coverage_complete':False,'coverage_scope':{'kind':'visible_shelves','retailer':retailer or '*','limit':500},'retailers':list(retailers.values()),'diagnostics':diagnostics,'warnings':list(dict.fromkeys(warnings))}
        except (BrowserTimeout,DeadlineExceeded):
            emit('search_failed',provider='instacart',stage=stage,status='provider_timeout',duration_ms=round((time.monotonic()-started)*1000))
            raise ConnectorError('provider_timeout','Instacart timed out while '+stage+'.')
        except ConnectorError as e:
            emit('search_failed',provider='instacart',stage=stage,status=e.status,duration_ms=round((time.monotonic()-started)*1000))
            raise
        finally:
            emit('search_finished',provider='instacart',duration_ms=round((time.monotonic()-started)*1000),item_count=len(items))
            context.close();browser.close()
