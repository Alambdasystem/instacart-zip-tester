"""Diagnostic public HTML fetch; never supplies verified offers or cart quotes."""
from html.parser import HTMLParser
from urllib.request import Request,urlopen
from urllib.error import HTTPError,URLError
from urllib.parse import quote
import time,re,socket
from connector import normalize
from telemetry import emit

from observation import Cards, visible_text
from migration_validation import validate_pair
from deadline import bounded_seconds, check, DeadlineExceeded

def parse_html(html,query,retailer=''):
    parser=Cards();parser.feed(html)
    cards=parser.cards+parser.hydration_cards()
    rows=normalize(cards,query,retailer,None)
    rows=list({i['id']:i for i in rows if not retailer or i['retailer']==retailer}.values())
    return [{**r,'source':'instacart_public_http','location_verified':False,'price_fresh':False,'selectable':False} for r in rows]

def search_http(z,query,retailer='',opener=urlopen):
    started=time.monotonic();url='https://www.instacart.com/store/'+(retailer+'/' if retailer else '')+'s?k='+quote(query)
    result={'status':'error','source':'direct_http','items':[],'location':{'requested_zip':z,'observed_zip':None,'verified':False},'cart_supported':False,'warnings':['Diagnostic HTML only. No delivery location was established; prices are not local verified offers. ZIP and address are not sent as undocumented parameters.']}
    try:
        # Public URL only, no cookies, private endpoints, or browser fingerprint changes.
        with opener(Request(url,headers={'Accept':'text/html'}),timeout=bounded_seconds(20)) as response:
            result['http_status']=response.status
            if 'text/html' not in response.headers.get('Content-Type',''):raise ValueError('content_type')
            chunks=[];total=0
            while total<=3_000_000:
                check();chunk=response.read(min(65536,3_000_001-total))
                if not chunk:break
                chunks.append(chunk);total+=len(chunk)
            raw=b''.join(chunks)
            if len(raw)>3000000:raise ValueError('response_too_large')
        html=raw.decode('utf-8',errors='replace');result['response_bytes']=len(raw)
        if re.search(r'verify you are human|checking your browser|unusual traffic|automated queries|access denied',visible_text(html),re.I):
            result.update(status='provider_blocked',error='Provider screening encountered; no retry or bypass attempted.')
        else:
            result['items']=parse_html(html,query,retailer);result['status']='ok' if result['items'] else 'no_readable_products'
    except HTTPError as e:result.update(http_status=e.code,error='Public HTTP request failed.',status='http_error')
    except (TimeoutError,socket.timeout):result.update(status='provider_timeout',error='Public HTTP fetch exceeded its socket timeout.')
    except URLError as e:result.update(status='network_error',error='Public HTTP fetch failed.')
    except ValueError:result.update(status='invalid_response',error='Unexpected response type or size.')
    result['duration_ms']=round((time.monotonic()-started)*1000)
    emit('direct_http_result',provider='instacart',status=result['status'],duration_ms=result['duration_ms'],item_count=len(result['items']),http_status=result.get('http_status'))
    return result

def compare_results(direct,browser):
    a={i['id']:i for i in direct.get('items',[])};b={i['id']:i for i in browser.get('items',[])};overlap=set(a)&set(b)
    def summary(result):
        rows=result.get('items',[])
        return {'status':result.get('status'),'products':len(rows),'with_prices':sum(isinstance(i.get('price'),(int,float)) for i in rows),'with_images':sum(bool(i.get('image_url')) for i in rows),'retailers':sorted({i.get('retailer','') for i in rows}),'duration_ms':result.get('duration_ms'),'cache_hit':result.get('cache_hit',False),'source':result.get('source'),'error':result.get('error'),'coverage_complete':result.get('coverage_complete',False),'location':result.get('location',{})}
    parity=validate_pair({'direct_http':direct,'browser':browser})
    comparable=parity['gates']['same_verified_context']
    return {'comparability':'comparable' if comparable else 'not_comparable','validation':parity,'direct_http':summary(direct),'browser':summary(browser),'matching_product_retailer_ids':len(overlap),'equal_prices_on_matching_ids':sum(a[k].get('price')==b[k].get('price') for k in overlap),'equivalent_for_local_search':parity['decision']=='PAIR_PARITY_ONLY','recommendation':'Keep normal browser and cache behavior. Direct HTML has no established delivery location and may contain only initial shelves.'}
