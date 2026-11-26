"""Full-history search, driven through a running `python -m atlas` over its HTTP API."""
import json
import random
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import closing
from http.cookiejar import CookieJar
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

# The schema as it was before search existed, without a version number.
PREVIOUS_SCHEMA="""
CREATE TABLE IF NOT EXISTS groups(id TEXT PRIMARY KEY,name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS sessions(
 id TEXT PRIMARY KEY,provider TEXT,account TEXT,source TEXT,cwd TEXT,url TEXT,title TEXT,
 group_id TEXT REFERENCES groups(id),position REAL DEFAULT 0,hidden INTEGER DEFAULT 0,
 state TEXT DEFAULT 'idle',latest TEXT DEFAULT '',latest_at REAL DEFAULT 0,seen INTEGER DEFAULT 0,
 live_at REAL DEFAULT 0,bridge TEXT DEFAULT '',writable INTEGER DEFAULT 0,synced_at REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS messages(
 seq INTEGER PRIMARY KEY AUTOINCREMENT,session TEXT REFERENCES sessions(id),source_id TEXT,
 role TEXT,body TEXT,at REAL,alert INTEGER, UNIQUE(session,source_id));
CREATE TABLE IF NOT EXISTS aliases(session TEXT,alias TEXT,seq INTEGER REFERENCES messages(seq),PRIMARY KEY(session,alias));
CREATE INDEX IF NOT EXISTS message_thread ON messages(session,at);
CREATE INDEX IF NOT EXISTS alias_target ON aliases(session,seq);
CREATE INDEX IF NOT EXISTS alerts ON messages(alert,seq);
CREATE INDEX IF NOT EXISTS inbox ON messages(role,at);
CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY,inode INTEGER,offset INTEGER,meta TEXT);
CREATE TABLE IF NOT EXISTS deliveries(
 id TEXT PRIMARY KEY,session TEXT REFERENCES sessions(id),body TEXT,status TEXT,
 detail TEXT DEFAULT '',created REAL,updated REAL,tab TEXT DEFAULT '');
CREATE INDEX IF NOT EXISTS pending ON deliveries(session,status,created);
CREATE TABLE IF NOT EXISTS prefs(key TEXT PRIMARY KEY,value TEXT);
"""


class Atlas:
    """One collector process on a private data directory, reached like the dashboard reaches it."""
    def __init__(self,data):
        self.data=data
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));self.port=sock.getsockname()[1]
        self.origin=f'http://127.0.0.1:{self.port}'
        self.proc=subprocess.Popen([sys.executable,'-m','atlas','--data',str(data),'--codex-dir',str(data/'none1'),
                                    '--claude-dir',str(data/'none2'),'--no-open','--port',str(self.port)],
                                   cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.web=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(CookieJar()))
        for _ in range(200):
            # A restart on the same data directory reads the previous launch URL until the new process replaces it.
            try:self.web.open((data/'launch.url').read_text().strip(),timeout=5).read();break
            except OSError:time.sleep(.05)
        else:raise RuntimeError('Thread Atlas did not start')
        self.bridge=self.get('pairing')['token']

    def get(self,action,**query):
        with self.web.open(self.origin+'/api/'+action+('?'+urllib.parse.urlencode(query) if query else ''),timeout=20) as r:
            return json.loads(r.read())

    def post(self,path,data,headers=None):
        request=urllib.request.Request(self.origin+path,json.dumps(data).encode(),{'Content-Type':'application/json',**(headers or {})})
        with self.web.open(request,timeout=60) as r:return json.loads(r.read())

    def search(self,q):return self.get('search',q=q)

    def sync(self,source,messages):
        return self.post('/bridge/sync',{'provider':'Claude','account':'Work','source':source,'tab':'tab-'+source+'-0123456789',
                                         'messages':messages},{'Authorization':'Bearer '+self.bridge})['session']

    def thread(self,uuid,name,*messages):
        """Import one Claude conversation and return its session ID."""
        chat=[{'uuid':uuid+'-'+str(n),'sender':role,'text':text,'created_at':'2025-03-%02dT12:00:00Z'%(n+1)} for n,(role,text) in enumerate(messages)]
        self.post('/api/import',{'provider':'Claude','account':'Work','data':[{'uuid':uuid,'name':name,'chat_messages':chat}]})
        return next(i['id'] for i in self.get('state')['sessions'] if i['title']==name)

    def seq(self,sid,body):
        return next(m['seq'] for m in self.get('history',id=sid) if m['body']==body)

    def stop(self):
        self.proc.terminate();self.proc.wait(timeout=20)


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.data=Path(self.tmp.name)/'data'

    def start(self):
        atlas=Atlas(self.data);self.addCleanup(atlas.stop);return atlas

    def version(self):
        with closing(sqlite3.connect(self.data/'atlas.sqlite')) as db,db:return db.execute('PRAGMA user_version').fetchone()[0]

    def test_search_finds_terms_only_in_older_message(self):
        atlas=self.start()
        older='Agreed payment retry policy: retry failed charges three times.'
        target=atlas.thread('billing','Billing sync notes',('human',older),('assistant','Noted.'),
                            ('human','Rename the CSV columns.'),('assistant','Renamed the CSV columns.'))
        decoy=atlas.thread('invoice','Invoice layout',('human','Make it green.'),('assistant','The payment button is now green.'))
        # Words that only begin like a query term ("retrieve" for "retry") do not match.
        atlas.thread('logs','Log export',('human','We will retrieve the logs tonight.'),('assistant','Retrieved.'))
        found=atlas.search('payment retry policy')
        self.assertEqual([found[0]['session'],found[0]['seq']],[target,atlas.seq(target,older)])
        self.assertIn(older,found[0]['snippet'])
        self.assertEqual([f['session'] for f in found],[target,decoy])

    def test_search_reflects_in_place_edit(self):
        atlas=self.start()
        sid=atlas.sync('live-edit',[{'id':'dom-1','role':'assistant','text':'The deploy window opens on Tuesday.'}])
        self.assertEqual([f['session'] for f in atlas.search('tuesday')],[sid])
        atlas.sync('live-edit',[{'id':'dom-1','role':'assistant','text':'The deploy window opens on Thursday.'}])
        self.assertEqual([m['body'] for m in atlas.get('history',id=sid)],['The deploy window opens on Thursday.'])
        self.assertEqual([(f['session'],f['snippet']) for f in atlas.search('thursday')],[(sid,'The deploy window opens on Thursday.')])
        self.assertEqual(atlas.search('tuesday'),[])

    def test_search_excludes_hidden_sessions(self):
        atlas=self.start()
        sid=atlas.thread('vault','Vault notes',('human','Rotate the quarterly credentials.'),('assistant','Done.'))
        self.assertEqual([f['session'] for f in atlas.search('quarterly credentials')],[sid])
        atlas.post('/api/edit',{'id':sid,'hidden':True})
        self.assertEqual(atlas.search('quarterly credentials'),[])

    def test_existing_database_is_migrated_and_backfilled_once(self):
        self.data.mkdir()
        with closing(sqlite3.connect(self.data/'atlas.sqlite')) as db,db:
            db.executescript(PREVIOUS_SCHEMA)
            db.execute("INSERT INTO groups VALUES('g','Payments')")
            db.execute("INSERT INTO sessions(id,provider,account,source,title,group_id) VALUES('old','Claude','Work','s1','Retry notes','g')")
            db.executemany("INSERT INTO messages(session,source_id,role,body,at,alert) VALUES('old',?,?,?,?,0)",
                           [('m1','user','How should failed webhooks back off?',1),('m2','assistant','Use jittered exponential backoff.',2)])
        self.assertEqual(self.version(),0)
        for _ in range(2):
            atlas=Atlas(self.data)
            try:
                found=atlas.search('jittered backoff')
                self.assertEqual([(f['session'],f['seq']) for f in found],[('old',2)])
            finally:atlas.stop()
            self.assertEqual(self.version(),1)
            with closing(sqlite3.connect(self.data/'atlas.sqlite')) as db,db:
                db.execute("INSERT INTO messages_fts(messages_fts) VALUES('integrity-check')")
                self.assertEqual(db.execute("SELECT count(*) FROM messages_fts WHERE messages_fts MATCH 'backoff'").fetchone()[0],1)
        self.data=Path(self.tmp.name)/'fresh'
        self.start().stop()
        self.assertEqual(self.version(),1)

    def test_newer_database_is_left_unchanged(self):
        self.start().stop()
        with closing(sqlite3.connect(self.data/'atlas.sqlite')) as db,db:db.execute('PRAGMA user_version=2')
        try:
            run=subprocess.run([sys.executable,'-m','atlas','--data',str(self.data),'--codex-dir',str(self.data/'none1'),
                                '--claude-dir',str(self.data/'none2'),'--no-open','--port','0'],cwd=ROOT,capture_output=True,text=True,timeout=30)
        except subprocess.TimeoutExpired:self.fail('Thread Atlas kept running on a database from a newer version')
        self.assertNotEqual(run.returncode,0)
        self.assertIn('Database schema version 2 is newer than this Thread Atlas supports',run.stderr)
        self.assertEqual(self.version(),2)

    def test_history_rejects_invalid_message_numbers(self):
        atlas=self.start()
        sid=atlas.thread('numbers','Number check',('human','Count the rows.'),('assistant','Counted.'))
        for seq in ('abc','0','123456789012345678901234567890'):
            with self.assertRaises(urllib.error.HTTPError) as caught:atlas.get('history',id=sid,seq=seq)
            self.assertEqual((caught.exception.code,json.loads(caught.exception.read())),(400,{'error':'Invalid message number'}))
        # A well-formed number that matches no message opens the latest messages.
        self.assertEqual([m['body'] for m in atlas.get('history',id=sid,seq='9999999999999999999')],['Count the rows.','Counted.'])

    def test_search_falls_back_to_common_terms(self):
        atlas=self.start()
        closed=atlas.thread('close','Month close',('human','The ledger is closed for March.'))
        audit=atlas.thread('audit','Audit prep',('human','The ledger needs a second review.'))
        vendors=atlas.thread('vendors','Vendor list',('human','Check the vendor names.'))
        # "ledger" is in most messages and "reconcile" in none, so the common term is what can still find a thread.
        self.assertEqual(sorted(f['session'] for f in atlas.search('ledger reconcile')),sorted([closed,audit]))
        # The same holds when the rare term is only in a hidden thread.
        atlas.post('/api/edit',{'id':vendors,'hidden':True})
        self.assertEqual(sorted(f['session'] for f in atlas.search('ledger vendor')),sorted([closed,audit]))

    def test_korean_lookup_matches_inflected_forms(self):
        atlas=self.start()
        body='결제 재시도 정책은 세 번으로 정했습니다.'
        sid=atlas.thread('korean','결제 메모',('human',body),('assistant','알겠습니다.'))
        atlas.thread('other','배포 메모',('human','배포는 목요일입니다.'),('assistant','확인했습니다.'))
        found=atlas.search('결제 재시도 정책 어디까지 했지?')
        self.assertEqual([(f['session'],f['seq']) for f in found],[(sid,atlas.seq(sid,body))])

    def test_search_keeps_threads_behind_a_busy_one(self):
        self.start().stop()
        with closing(sqlite3.connect(self.data/'atlas.sqlite')) as db,db:
            db.execute("INSERT INTO groups VALUES('g','Books')")
            db.executemany("INSERT INTO sessions(id,provider,account,source,title,group_id) VALUES(?,'Claude','Books',?,?,'g')",
                           [(n,n,n.title()) for n in ('busy','quiet','other','notes')])
            db.executemany("INSERT INTO messages(session,source_id,role,body,at,alert) VALUES('notes',?,'user',?,?,0)",
                           [(str(n),'Unrelated note '+str(n)+'.',n) for n in range(2000)])
            # One thread mentions the topic in 1,500 short messages, each a stronger match than the other threads' one.
            db.executemany("INSERT INTO messages(session,source_id,role,body,at,alert) VALUES('busy',?,'assistant',?,?,0)",
                           [(str(n),'Ledger reconciliation run '+str(n)+'.',n) for n in range(1500)])
            db.executemany("INSERT INTO messages(session,source_id,role,body,at,alert) VALUES(?,'1','user',?,2000,0)",
                           [(n,'Before closing the books we still owe the '+n+' ledger reconciliation and a review of the vendor list.')
                            for n in ('quiet','other')])
        found=self.start().search('ledger reconciliation')
        self.assertEqual([found[0]['session'],sorted(f['session'] for f in found[1:])],['busy',['other','quiet']])

    def test_search_is_fast_on_100k_messages(self):
        self.start().stop()
        rng=random.Random(7)
        vocabulary=[''.join(rng.choice('bcdfghklmnprstvz')+rng.choice('aeiou') for _ in range(rng.randint(2,4))) for _ in range(5000)]
        # The query's topic words each appear in a few percent of the messages, as a busy project's terms would.
        vocabulary+=['payment']*8+['retry']*5
        # Words like these turn up in most messages of a coding project.
        vocabulary+=['test','error','code','file']*200
        with closing(sqlite3.connect(self.data/'atlas.sqlite')) as db,db:
            db.execute("INSERT INTO groups VALUES('g','Load')")
            db.executemany("INSERT INTO sessions(id,provider,account,source,title,group_id) VALUES(?,'Claude','Load',?,?,'g')",
                           [('s'+str(n),'s'+str(n),'Load '+str(n)) for n in range(1000)])
            db.executemany("INSERT INTO messages(session,source_id,role,body,at,alert) VALUES(?,?,?,?,?,0)",
                           (('s'+str(n%1000),str(n),'assistant' if n%2 else 'user',' '.join(rng.choices(vocabulary,k=rng.randint(12,300))),n)
                            for n in range(100000)))
            # One thread holds most of the matches for a topic, and each of them outranks every other thread's match.
            db.executemany("INSERT INTO messages(session,source_id,role,body,at,alert) VALUES('s0',?,'assistant',?,?,0)",
                           (('ledger-'+str(n),'Ledger reconciliation run '+str(n)+'.',100000+n) for n in range(20000)))
            db.executemany("INSERT INTO messages(session,source_id,role,body,at,alert) VALUES(?,'ledger','user',?,120000,0)",
                           (('s'+str(n),' '.join(rng.choices(vocabulary,k=100))+' ledger reconciliation') for n in range(1,100)))
        atlas=self.start()
        for query in ['payment retry policy','where did we leave the test error in the code file','ledger reconciliation']:
            self.assertEqual(len(atlas.search(query)),20)
            timings=[]
            for _ in range(5):
                start=time.perf_counter();atlas.search(query);timings.append(time.perf_counter()-start)
            self.assertLess(statistics.median(timings),.1,(query,timings))


if __name__=='__main__':unittest.main()
