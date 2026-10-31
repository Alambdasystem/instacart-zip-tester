"""Additive normalized offer storage; legacy rows remain for rollback."""
import hashlib,json
DESCRIPTIVE={'name','image_url','brand'}
CORE={'id','price','currency','size','retailer','provider_location_id','availability','provider','context_id','requested_zip','observed_zip','location_verified','branch_verified','delivery_verified','fulfillment','product_key','catalog_key','provider_context'}
class OfferStore:
    @staticmethod
    def schema(db):
        db.executescript('''CREATE TABLE IF NOT EXISTS offer_products(id TEXT PRIMARY KEY,provider TEXT NOT NULL,canonical_url TEXT,description TEXT NOT NULL,seen REAL NOT NULL);
CREATE TABLE IF NOT EXISTS offer_contexts(id TEXT PRIMARY KEY,context_id TEXT,requested_zip TEXT,observed_zip TEXT,verified INTEGER,branch_verified INTEGER,delivery_verified INTEGER,fulfillment TEXT,metadata TEXT);
CREATE TABLE IF NOT EXISTS offers(id TEXT PRIMARY KEY,product_id TEXT NOT NULL,context_key TEXT NOT NULL,provider TEXT NOT NULL,retailer TEXT,item_id TEXT,store_id TEXT,price REAL,currency TEXT,size TEXT,availability TEXT,seen REAL NOT NULL,expires REAL NOT NULL,terms TEXT NOT NULL,attributes TEXT NOT NULL,FOREIGN KEY(product_id) REFERENCES offer_products(id),FOREIGN KEY(context_key) REFERENCES offer_contexts(id));
CREATE INDEX IF NOT EXISTS offers_provider_expiry ON offers(provider,expires);
CREATE INDEX IF NOT EXISTS offer_context_lookup ON offer_contexts(context_id,requested_zip);''')
        if 'metadata' not in {r[1] for r in db.execute('PRAGMA table_info(offer_contexts)')}:db.execute("ALTER TABLE offer_contexts ADD COLUMN metadata TEXT DEFAULT '{}'")
    @staticmethod
    def put(db,key,p,terms,seen,expires):
        provider=p.get('provider','instacart');canonical=(p.get('url') or p.get('id','')).split('?')[0]
        product=p.get('product_key') or hashlib.sha256((provider+'|'+canonical).encode()).hexdigest()
        ctx={k:p.get(k) for k in ('context_id','requested_zip','observed_zip','location_verified','branch_verified','delivery_verified','fulfillment')}
        ctx['provider_context']=p.get('provider_context',{})
        context_key=hashlib.sha256(json.dumps([provider,ctx],sort_keys=True).encode()).hexdigest()
        desc={k:p.get(k) for k in DESCRIPTIVE}
        attrs={k:v for k,v in p.items() if k not in DESCRIPTIVE|CORE}
        # Product URL includes the retailer handoff and therefore belongs to the offer.
        attrs['url']=p.get('url')
        db.execute('INSERT INTO offer_products VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET description=excluded.description,seen=excluded.seen',(product,provider,canonical,json.dumps(desc),seen))
        db.execute('INSERT OR IGNORE INTO offer_contexts VALUES(?,?,?,?,?,?,?,?,?)',(context_key,ctx['context_id'],ctx['requested_zip'],ctx['observed_zip'],int(bool(ctx['location_verified'])),int(bool(ctx['branch_verified'])),int(bool(ctx['delivery_verified'])),ctx['fulfillment'],json.dumps(ctx['provider_context'])))
        db.execute('INSERT OR REPLACE INTO offers VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(key,product,context_key,provider,p.get('retailer',''),p.get('id'),p.get('provider_location_id'),p.get('price'),p.get('currency'),p.get('size'),p.get('availability'),seen,expires,terms,json.dumps(attrs)))
    @staticmethod
    def rows(db,zip_code=None,retailer=None,provider=None,context=None,limit=5000):
        where=[];args=[]
        for column,value in [('c.requested_zip',zip_code),('o.retailer',retailer),('o.provider',provider),('c.context_id',context)]:
            if value:where.append(column+'=?');args.append(value)
        sql='''SELECT o.id,o.product_id,o.provider,o.retailer,o.item_id,o.store_id,o.price,o.currency,o.size,o.availability,o.seen,o.expires,o.terms,o.attributes,p.description,c.context_id,c.requested_zip,c.observed_zip,c.verified,c.branch_verified,c.delivery_verified,c.fulfillment,c.metadata FROM offers o JOIN offer_products p ON p.id=o.product_id JOIN offer_contexts c ON c.id=o.context_key'''+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY o.seen DESC LIMIT ?'
        for row in db.execute(sql,args+[limit]):
            key,product,prov,store,item_id,store_id,price,currency,size,availability,seen,expires,terms,attrs,desc,ctx,z,observed,verified,branch,delivery,fulfillment,metadata=row
            p={**json.loads(desc),**json.loads(attrs),'id':item_id,'catalog_key':key,'product_key':product,'provider':prov,'retailer':store,'provider_location_id':store_id,'price':price,'currency':currency,'size':size,'availability':availability,'context_id':ctx,'requested_zip':z,'observed_zip':observed,'location_verified':bool(verified),'branch_verified':bool(branch),'delivery_verified':bool(delivery),'fulfillment':fulfillment,'provider_context':json.loads(metadata or '{}')}
            yield json.dumps(p),seen,expires,terms,prov
    @staticmethod
    def prune(db):
        db.execute('DELETE FROM offers WHERE id IN (SELECT id FROM offers ORDER BY seen DESC LIMIT -1 OFFSET 5000)')
        db.execute('DELETE FROM offer_contexts WHERE id NOT IN (SELECT context_key FROM offers)')
        db.execute('DELETE FROM offer_products WHERE id NOT IN (SELECT product_id FROM offers) AND id IN (SELECT id FROM offer_products ORDER BY seen DESC LIMIT -1 OFFSET 10000)')
