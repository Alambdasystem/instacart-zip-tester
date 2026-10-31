"""Offline migration gate for saved /diagnostics/http-compare JSON. Never calls providers."""
import argparse,json
from html.parser import HTMLParser
from urllib.parse import unquote
from decimal import Decimal,InvalidOperation

def audit_html(text):
    class Audit(HTMLParser):
        def __init__(self):super().__init__();self.cards=0;self.scripts=[];self.current=None
        def handle_starttag(self,tag,attrs):
            attrs=dict(attrs)
            if tag=='a' and attrs.get('data-item-card-button')=='true':self.cards+=1
            if tag=='script' and attrs.get('type')=='application/json':self.current=''
        def handle_data(self,data):
            if self.current is not None:self.current+=data
        def handle_endtag(self,tag):
            if tag=='script' and self.current is not None:self.scripts.append(self.current);self.current=None
    parser=Audit();parser.feed(text);ids=set();references=set();groups=set();valid=0
    def walk(value):
        if isinstance(value,dict):
            if value.get('__typename')=='ItemsItem' and value.get('id'):ids.add(value['id'])
            if value.get('__typename')=='SearchRetailerResults':
                groups.add((value.get('retailerId'),value.get('shopId')));references.update(value.get('itemIds') or [])
            for child in value.values():walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    for script in parser.scripts:
        try:payload=json.loads(unquote(script))
        except (ValueError,TypeError):continue
        valid+=1;walk(payload)
    return {'html_card_anchors':parser.cards,'decoded_json_blocks':valid,'retailer_result_groups':len(groups),'unique_item_references':len(references),'fully_populated_unique_items':len(ids),'references_missing_item_details':len(references-ids)}

def validate_pair(data):
    direct=data.get('direct_http') or {};browser=data.get('browser') or {}
    a={i['id']:i for i in direct.get('items',[]) if i.get('id')};b={i['id']:i for i in browser.get('items',[]) if i.get('id')}
    la=direct.get('location') or {};lb=browser.get('location') or {}
    # Counts alone can't establish the same address/session or complete offers.
    same_context=bool(la.get('verified') and lb.get('verified') and la.get('context_id') and la.get('context_id')==lb.get('context_id') and la.get('observed_zip')==lb.get('observed_zip'))
    overlap=set(a)&set(b)
    def money(value):
        try:
            result=Decimal(str(value));return result if result.is_finite() else None
        except (InvalidOperation,ValueError):return None
    def same_offer(k):
        x,y=a[k],b[k]
        return (money(x.get('price')) is not None and money(x.get('price'))==money(y.get('price')) and x.get('currency') and x.get('currency')==y.get('currency') and x.get('size')==y.get('size') and x.get('retailer')==y.get('retailer'))
    gates={
      'successful_nonempty_results':bool(direct.get('status')=='ok' and browser.get('status')=='ok' and a and b),
      'same_verified_context':same_context,
      'same_product_retailer_ids':bool(a and set(a)==set(b)),
      'same_price_size_currency':bool(overlap and all(same_offer(k) for k in overlap) and set(a)==set(b)),
      'image_coverage':bool(a and all(i.get('image_url') for i in a.values()) and set(a)==set(b)),
      'coverage_proven':direct.get('coverage_complete') is True and browser.get('coverage_complete') is True,
    }
    return {'decision':'BLOCKED' if not all(gates.values()) else 'PAIR_PARITY_ONLY','gates':gates,'direct_products':len(a),'browser_products':len(b),'overlap':len(overlap),'matching_offers':sum(same_offer(k) for k in overlap),'release_approved':False,'note':'Pair parity alone never approves rollout. Complete the repeated matrix, cache, cart, resource and rollback checklist.'}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('comparison_json');parser.add_argument('--html',action='store_true');args=parser.parse_args()
    with open(args.comparison_json) as f:report=audit_html(f.read()) if args.html else validate_pair(json.load(f))
    print(json.dumps(report,indent=2));return 2 if report.get('decision')=='BLOCKED' else 0
if __name__=='__main__':raise SystemExit(main())
