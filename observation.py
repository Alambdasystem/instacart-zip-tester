"""Public HTML observations only: cards and complete embedded item records."""
from html.parser import HTMLParser
from urllib.parse import unquote,urlsplit
import json,re
class Cards(HTMLParser):
    def __init__(self):
        super().__init__();self.cards=[];self.active=None;self.skip=0;self.heading=False;self.scripts=[];self.script=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in ('script','style'):self.skip+=1
        if tag=='script' and a.get('type')=='application/json':self.script=''
        if self.skip:return
        if tag=='a' and a.get('data-item-card-button')=='true' and a.get('href','').startswith('/products/'):
            self.active={'url':a['href'],'name':a.get('aria-label',''),'image_url':None,'parts':[],'heading':[]}
        if self.active:
            if tag in ('h2','h3') or a.get('role')=='heading':self.heading=True
            if tag=='img':
                self.active['name']=self.active['name'] or a.get('alt','')
                self.active['image_url']=a.get('src') or (a.get('srcset','').split() or [None])[0]
    def handle_endtag(self,tag):
        if tag=='script' and self.script is not None:self.scripts.append(self.script);self.script=None
        if tag in ('script','style'):self.skip=max(0,self.skip-1)
        if tag in ('h2','h3'):self.heading=False
        if tag=='a' and self.active:
            c=self.active;c['name']=' '.join(c.pop('heading')) or c['name'];c['text']='\n'.join(c.pop('parts'));self.cards.append(c);self.active=None;self.heading=False
    def handle_data(self,data):
        if self.script is not None:self.script+=data
        if self.active and not self.skip and data.strip():
            self.active['parts'].append(data.strip())
            if self.heading:self.active['heading'].append(data.strip())
    def hydration_cards(self):
        nodes=[]
        def walk(x):
            if isinstance(x,dict):
                nodes.append(x)
                for v in x.values():walk(v)
            elif isinstance(x,list):
                for v in x:walk(v)
        for s in self.scripts:
            try:walk(json.loads(unquote(s)))
            except (ValueError,RecursionError):continue
        stores={x.get('id'):x.get('slug') for x in nodes if x.get('__typename')=='RetailersRetailer' and x.get('slug')}
        itemstores={}
        for x in nodes:
            if x.get('__typename')=='SearchRetailerResults' and stores.get(x.get('retailerId')):
                for i in x.get('itemIds',[]):itemstores[i]=stores[x['retailerId']]
        output=[];seen=set()
        for x in nodes:
            if x.get('__typename')!='ItemsItem' or x.get('id') in seen:continue
            seen.add(x.get('id'));store=itemstores.get(x.get('id'))
            url=x.get('evergreenUrl');price=((x.get('price') or {}).get('viewSection') or {}).get('itemCard') or {}
            price=price.get('priceScreenReaderString') or price.get('priceAriaLabelString') or price.get('priceString')
            if not store or not url or not x.get('name') or not price:continue
            image=((x.get('viewSection') or {}).get('itemImage') or {}).get('templateUrl')
            if image:
                for k in ('width','height'):image=image.replace('{'+k+'}','197')
                if '{' in image:image=None
            output.append({'name':x['name'],'text':'Current price: '+price.replace('Current price:','').strip()+'\n'+str(x.get('size') or ''),'url':'/products/'+url+'?retailerSlug='+store,'image_url':image})
        return output

def visible_text(html):
    class Text(HTMLParser):
        def __init__(self):super().__init__();self.skip=0;self.parts=[]
        def handle_starttag(self,t,a):
            if t in ('script','style'):self.skip+=1
        def handle_endtag(self,t):
            if t in ('script','style'):self.skip=max(0,self.skip-1)
        def handle_data(self,d):
            if not self.skip:self.parts.append(d)
    p=Text();p.feed(html);return ' '.join(p.parts)
