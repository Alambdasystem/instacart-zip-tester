"""Normalized persistent scheduler jobs with a retained legacy rollback snapshot."""
import hashlib,json
FIELDS=['query','zip','retailer','requests','next_due','successes','failures','last_status','last_success','last_attempt','last_requested']
class JobStore:
    @staticmethod
    def schema(db):
        db.execute('CREATE TABLE IF NOT EXISTS ingestion_jobs(worker TEXT,job_id TEXT,position INTEGER,query TEXT,zip TEXT,retailer TEXT,requests INTEGER,next_due REAL,successes INTEGER,failures INTEGER,last_status TEXT,last_success REAL,last_attempt REAL,last_requested REAL,extras TEXT,PRIMARY KEY(worker,job_id))')
        db.execute('CREATE INDEX IF NOT EXISTS ingestion_jobs_due ON ingestion_jobs(worker,next_due)')
    @staticmethod
    def save(db,worker,jobs):
        keep=[];duplicates={}
        existing={r[0]:tuple(r[1:]) for r in db.execute('SELECT job_id,position,'+','.join(FIELDS)+',extras FROM ingestion_jobs WHERE worker=?',(worker,))}
        for position,j in enumerate(jobs):
            base=hashlib.sha256(json.dumps([j['zip'],j['retailer'],j['query'].casefold()]).encode()).hexdigest();n=duplicates.get(base,0);duplicates[base]=n+1;key=base if not n else base+':duplicate:'+str(n);keep.append(key)
            extras={k:v for k,v in j.items() if k not in FIELDS}
            # Absent optional values stay absent when reconstructed.
            extras['_present']=[k for k in FIELDS if k in j]
            values=[position]+[j.get(k) for k in FIELDS]+[json.dumps(extras)]
            if existing.get(key)!=tuple(values):db.execute('INSERT OR REPLACE INTO ingestion_jobs VALUES('+','.join('?' for _ in range(15))+')',[worker,key]+values)
        present={r[0] for r in db.execute('SELECT job_id FROM ingestion_jobs WHERE worker=?',(worker,))}
        for key in present-set(keep):db.execute('DELETE FROM ingestion_jobs WHERE worker=? AND job_id=?',(worker,key))
    @staticmethod
    def load(db,worker):
        jobs=[]
        for row in db.execute('SELECT '+','.join(FIELDS)+',extras FROM ingestion_jobs WHERE worker=? ORDER BY position',(worker,)):
            extras=json.loads(row[-1]);present=extras.pop('_present',FIELDS)
            jobs.append({**extras,**{k:v for k,v in zip(FIELDS,row[:-1]) if k in present}})
        return jobs
