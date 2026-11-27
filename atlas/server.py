"""Loopback dashboard, transcript collector, durable dispatch and opt-in browser bridge."""
import argparse
import asyncio
import fcntl
import hmac
import json
import os
import re
import secrets
import sys
import time
import uuid
import webbrowser
from pathlib import Path

from aiohttp import web

from .codex import Codex, output
from .indexer import epoch, import_export, scan
from .store import Store, ident

ROOT=Path(__file__).resolve().parent.parent
WEB_PROVIDERS=('ChatGPT','Claude')


class Service:
    def __init__(self,data,codex_dir,claude_dir,port,notifications=False):
        data.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.lock=(data/'service.lock').open('a')
        fcntl.flock(self.lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        self.store=Store(data/'atlas.sqlite')
        self.store.db.execute("UPDATE deliveries SET status='uncertain',detail='Service restarted during dispatch' WHERE status IN ('sending','claimed')")
        # A request that was still waiting when the service stopped is never sent later without the user asking again.
        self.store.db.execute("UPDATE deliveries SET status='failed',detail='Service restarted before dispatch' WHERE status='queued' AND tab=''")
        self.store.db.execute("UPDATE sessions SET live_at=0,bridge='',writable=0")
        self.store.db.execute("UPDATE deliveries SET status='uncertain',detail='Reconnect to verify the reply' WHERE status='delivered' AND tab!=''")
        self.store.db.execute("UPDATE deliveries SET status='uncertain',detail='Service restarted before the reply was confirmed' WHERE status='delivered' AND tab=''")
        self.codex_dir,self.claude_dir=codex_dir,claude_dir
        self.port=port;self.token=secrets.token_urlsafe(32);self.login=secrets.token_urlsafe(32)
        self.bridge=self.store.pref('bridge') or secrets.token_urlsafe(32)
        self.store.db.execute("INSERT OR IGNORE INTO prefs VALUES('bridge',?)",(self.bridge,))
        self.data=data;self.tasks=set();self.workers={};self.notifications=notifications
        self.scan_status='Indexing';self.codex_ok=False;self.claude_ok=False;self.last_scan=0;self.owned_terminals=set()
        self.dispatch_note=''
        self.notification_seq=self.store.db.execute("SELECT COALESCE(max(seq),0) FROM messages").fetchone()[0]

    def spawn(self,coro):
        task=asyncio.create_task(coro);self.tasks.add(task);task.add_done_callback(self.tasks.discard);return task

    async def loop(self):
        while True:
            try:
                await asyncio.to_thread(scan,self.store,self.codex_dir,self.claude_dir)
                self.last_scan=time.time();self.scan_status='Ready'
                for row in self.store.db.execute("""SELECT m.seq,s.title,s.provider,s.hidden FROM messages m JOIN sessions s ON s.id=m.session
                    WHERE m.alert=1 AND m.seq>? ORDER BY m.seq""",(self.notification_seq,)).fetchall():
                    self.notification_seq=row['seq']
                    if not row['hidden']:await self.notify(row['seq'],row['title'],row['provider'])
            except Exception as e:self.scan_status='Index unavailable: '+type(e).__name__
            await asyncio.sleep(2)

    async def mac_notification(self,seq,title,provider):
        helper_name = 'Thread Atlas Alerts.app'
        if ROOT.parent.name == 'Resources' and ROOT.parent.parent.name == 'Contents':
            helper = ROOT.parent.parent / 'Helpers' / helper_name
        else:
            helper = self.data / helper_name
            source = ROOT / 'macos' / 'alerts.swift'
            builder = ROOT / 'macos' / 'build-alerts.sh'
            executable = helper / 'Contents' / 'MacOS' / 'Alerts'
            if not executable.exists() or executable.stat().st_mtime < max(source.stat().st_mtime,builder.stat().st_mtime):
                await output(str(builder),str(helper),timeout=90)
        events = self.data / 'notification-events'
        events.mkdir(mode=0o700,exist_ok=True)
        event = events / (str(seq) + '.atlasreply')
        event.write_text(json.dumps({'title':title,'provider':provider}),encoding='utf-8')
        try:
            await output('/usr/bin/open','-g','-a',str(helper),str(event.resolve()))
        except Exception:
            event.unlink(missing_ok=True)
            raise

    async def notify(self,seq,title,provider):
        if self.store.pref('notifications','1' if self.notifications else '0')!='1':return
        if sys.platform == 'darwin':
            try:await self.mac_notification(seq,title,provider)
            except (OSError,RuntimeError,asyncio.TimeoutError) as e:
                print('Could not post macOS notification: '+str(e),file=sys.stderr)
        token=os.environ.get('ATLAS_TELEGRAM_TOKEN');chat=os.environ.get('ATLAS_TELEGRAM_CHAT')
        if token and chat:
            import aiohttp
            try:
                async with aiohttp.ClientSession() as client:
                    async with client.post(f'https://api.telegram.org/bot{token}/sendMessage',json={'chat_id':chat,'text':'Thread Atlas · '+title},timeout=8) as result:
                        result.raise_for_status()
            except Exception:pass

    async def capabilities(self):
        while True:
            try:
                async with Codex() as rpc:
                    await rpc.call('thread/list',{'limit':1});self.codex_ok=True
            except Exception:self.codex_ok=False
            try:
                auth=json.loads(await output('claude','auth','status',timeout=10))
                agents=json.loads(await output('claude','agents','--json',timeout=15))
                if not isinstance(agents,list):raise ValueError('Unsupported agent inventory')
                self.owned_terminals={a.get('sessionId') for a in agents}
                self.claude_ok=bool(auth.get('loggedIn'))
            except Exception:self.claude_ok=False
            await asyncio.sleep(45)

    def snapshot(self, after=None):
        state=self.store.state()
        locked=set()
        if self.owned_terminals:
            locked={r['id'] for r in self.store.db.execute("SELECT id,source FROM sessions WHERE provider='Claude Code'")
                    if r['source'] in self.owned_terminals}
        for i in state['sessions']:
            i['owned_terminal']=i['id'] in locked
            i['direct']=not i['owned_terminal'] and {'Codex':self.codex_ok,'Claude Code':self.claude_ok}.get(i['provider'],bool(i['web_live'] and i['writable']))
        state['events'],state['cursor']=self.store.alerts_after(after)
        state.update(scan=self.scan_status,lastScan=self.last_scan,codex=self.codex_ok,claude=self.claude_ok,
                     notifications=self.store.pref('notifications','1' if self.notifications else '0')=='1')
        return state

    def update(self,key,status,detail=''):
        self.store.db.execute("UPDATE deliveries SET status=?,detail=?,updated=? WHERE id=?",(status,detail[:500],time.time(),key))

    def enqueue(self,data):
        sid,key,body=data.get('session'),data.get('id'),data.get('body')
        if not isinstance(body,str) or not body.strip() or len(body)>50000:raise ValueError('Invalid message')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_-]{12,80}',key):raise ValueError('Invalid draft ID')
        with self.store.tx():
            old=self.store.db.execute("SELECT * FROM deliveries WHERE id=?",(key,)).fetchone()
            if old:
                if old['session']!=sid or old['body']!=body:raise ValueError('Draft ID already used')
                return dict(old)
            s=self.store.session_row(sid)
            if s['hidden']:raise ValueError('Session is hidden')
            if s['provider'] in WEB_PROVIDERS and (not s['bridge'] or not s['writable'] or time.time()-s['live_at']>35):raise ValueError('Web thread is offline or read-only')
            if s['provider']=='Codex' and not self.codex_ok:raise ValueError('Codex App Server is offline')
            if s['provider']=='Claude Code' and not self.claude_ok:raise ValueError('Claude Code is offline')
            self.store.db.execute("INSERT INTO deliveries(id,session,body,status,created,updated,tab) VALUES(?,?,?,'queued',?,?,?)",
                                  (key,sid,body,time.time(),time.time(),s['bridge']))
        return {'id':key,'status':'queued','session':sid}

    async def dispatch(self):
        while True:
            # A busy database or one bad row must not end dispatch for the rest of the run; the next tick retries.
            try:self.sweep();self.dispatch_note=''
            except Exception as e:
                note=type(e).__name__+': '+str(e)
                if note!=self.dispatch_note:self.dispatch_note=note;print('Dispatch paused · '+note,file=sys.stderr)
            await asyncio.sleep(.3)

    def sweep(self):
        now=time.time();db=self.store.db
        db.execute("""UPDATE deliveries SET status='uncertain',detail='Tab disconnected before acknowledgment'
          WHERE status IN ('claimed','delivered') AND tab!='' AND updated<? AND NOT EXISTS (SELECT 1 FROM sessions s WHERE s.id=deliveries.session AND s.live_at>?)""",(now-65,now-35))
        # A claim the tab never acknowledged would otherwise hold the thread's queue for as long as the tab stays open.
        db.execute("""UPDATE deliveries SET status='uncertain',detail='No acknowledgment from the tab'
          WHERE status='claimed' AND tab!='' AND updated<?""",(now-120,))
        # A request waits while its tab is connected and busy. It fails once that tab is gone, or after thirty minutes.
        db.execute("""UPDATE deliveries SET status='failed',detail=CASE WHEN created<? THEN 'Thread stayed busy' ELSE 'Tab disconnected before dispatch' END
          WHERE status='queued' AND tab!='' AND (created<? OR (created<? AND NOT EXISTS
            (SELECT 1 FROM sessions s WHERE s.id=deliveries.session AND s.bridge=deliveries.tab AND s.live_at>?)))""",(now-1800,now-1800,now-75,now-35))
        rows=db.execute("""SELECT d.*,s.provider,s.source,s.cwd,s.hidden FROM deliveries d JOIN sessions s ON d.session=s.id
         WHERE d.status='queued' AND s.provider NOT IN ('ChatGPT','Claude') ORDER BY d.created""").fetchall()
        for row in rows:
            sid=row['session']
            if sid not in self.workers:
                self.workers[sid]=self.spawn(self.local_send(dict(row)))

    async def local_send(self,d):
        key=d['id'];started=False
        try:
            if d['hidden']:raise RuntimeError('Session is hidden')
            uuid.UUID(d['source'])
            if d['provider']=='Codex':
                async with Codex() as c:
                    current=(await c.call('thread/read',{'threadId':d['source']}))['thread']
                    if current['status']['type']=='active':raise RuntimeError('Thread is active. Wait for the current turn.')
                    self.update(key,'sending');started=True
                    turn=await c.send(d['source'],d['body'])
                    self.update(key,'delivered',turn)
                    for _ in range(7200):
                        await asyncio.sleep(1)
                        result=await c.call('thread/turns/list',{'threadId':d['source'],'limit':8,'sortDirection':'desc','itemsView':'notLoaded'})
                        own=next((t for t in result['data'] if t['id']==turn),None)
                        if own and own['status'] in ('completed','failed','interrupted'):
                            self.update(key,'answered' if own['status']=='completed' else 'failed',turn);break
                    else:raise RuntimeError('The turn did not finish within two hours')
            else:
                agents=json.loads(await output('claude','agents','--json',timeout=15))
                if not isinstance(agents,list):raise RuntimeError('Unable to determine session ownership')
                if any(a.get('sessionId')==d['source'] for a in agents):raise RuntimeError('Session is open in another terminal; close it before resuming')
                cwd=d['cwd']
                if not cwd or not Path(cwd).is_dir():raise RuntimeError('Original working directory is unavailable')
                self.update(key,'sending');started=True
                raw=await output('claude','--print','--resume',d['source'],'--output-format','json','--',d['body'],cwd=cwd,timeout=3600)
                result=json.loads(raw)
                if result.get('session_id')!=d['source'] or result.get('is_error'):raise RuntimeError('Could not verify the original Claude session')
                self.update(key,'answered',d['source'])
        except Exception as e:
            old=self.store.db.execute("SELECT status FROM deliveries WHERE id=?",(key,)).fetchone()[0]
            note=str(e) or type(e).__name__
            # A receipt that already reached a final state stays; one that was delivered but never confirmed must not rest there.
            if old=='delivered':self.update(key,'uncertain','Delivered, but the reply was not confirmed: '+note)
            elif old not in ('answered','failed'):self.update(key,'uncertain' if started else 'failed',note)
            if old=='delivered' or (started and old not in ('answered','failed')):
                self.store.db.execute("""UPDATE deliveries SET status='failed',detail='An earlier message to this thread was not confirmed',updated=?
                  WHERE session=? AND status='queued' AND tab=''""",(time.time(),d['session']))
        finally:self.workers.pop(d['session'],None)

    def web_sync(self,d):
        provider,account,source,tab=d.get('provider'),d.get('account'),d.get('source'),d.get('tab')
        if provider not in WEB_PROVIDERS or not isinstance(account,str) or not 0<len(account.strip())<=120:raise ValueError('Invalid web account')
        if not isinstance(source,str) or not re.fullmatch(r'[\w-]{1,160}',source) or not isinstance(tab,str) or not re.fullmatch(r'[\w-]{12,100}',tab):raise ValueError('Invalid web thread')
        account=account.strip();url=('https://chatgpt.com/c/' if provider=='ChatGPT' else 'https://claude.ai/chat/')+source
        path=d.get('pathname','')
        if provider=='ChatGPT' and re.fullmatch(r'/g/[\w-]+/c/'+re.escape(source),path):
            url='https://chatgpt.com'+path
        with self.store.tx():
            sid=self.store.session(provider,account,source,provider+' · '+account,str(d.get('title') or '')[:180],url=url)
            s=self.store.session_row(sid)
            if s['bridge'] and s['bridge']!=tab and time.time()-s['live_at']<35:raise ValueError('Thread already connected in another tab')
            self.store.db.execute("UPDATE sessions SET live_at=?,bridge=?,writable=?,state=? WHERE id=?",(time.time(),tab,int(bool(d.get('writable'))),'run' if d.get('busy') else 'idle',sid))
            # A page shows no send time. History read on connect is filed after what is already known, so it neither
            # looks new nor outranks real arrivals in the inbox. Only messages seen arriving are stamped with the clock.
            initial=bool(d.get('initial'));seen={}
            known=self.store.db.execute("SELECT COALESCE(max(at),0) FROM messages WHERE session=?",(sid,)).fetchone()[0] if initial else 0
            for m in d.get('messages',[])[:160]:
                if not isinstance(m,dict):continue
                body=m.get('text');role=m.get('role');mid=m.get('id')
                if not isinstance(body,str) or role not in ('user','assistant') or not isinstance(mid,str):continue
                stamp=epoch(m.get('at'))
                self.store.merge_message(sid,mid,role,body,stamp or (known if initial else time.time()),
                                         initial,seen,not initial,estimated=not stamp)
            if initial:self.store.finish_batch(sid,seen)
            job=None
            if not d.get('busy') and d.get('writable') and not s['hidden'] and not self.store.db.execute("SELECT 1 FROM deliveries WHERE session=? AND status IN ('claimed','delivered') AND tab!=''",(sid,)).fetchone():
                row=self.store.db.execute("SELECT id,body FROM deliveries WHERE session=? AND tab=? AND status='queued' ORDER BY created LIMIT 1",(sid,tab)).fetchone()
                if row:job=dict(row);self.update(job['id'],'claimed')
            return {'session':sid,'job':job}

    def ack(self,d):
        old=self.store.db.execute("SELECT d.*,s.source FROM deliveries d JOIN sessions s ON s.id=d.session WHERE d.id=?",(d.get('id'),)).fetchone()
        if not old or old['tab']!=d.get('tab') or old['source']!=d.get('source'):raise ValueError('Unknown delivery for this tab')
        status=d.get('status')
        if status not in ('answered','delivered','uncertain','failed'):raise ValueError('Invalid status')
        if old['status'] in ('claimed','delivered','uncertain'):
            self.update(old['id'],status,str(d.get('detail','')))
        return {'ok':True}


def application(s):
    @web.middleware
    async def guard(request,next_handler=None,handler=None):
        handler=handler or next_handler
        if request.host!=f'127.0.0.1:{s.port}':raise web.HTTPForbidden()
        origin=request.headers.get('Origin','')
        bridge=request.path.startswith('/bridge/')
        if bridge:
            if not hmac.compare_digest(request.headers.get('Authorization',''),'Bearer '+s.bridge):raise web.HTTPUnauthorized()
        elif request.path.startswith('/launch/'):
            if not hmac.compare_digest(request.path.split('/')[-1],s.login):raise web.HTTPUnauthorized()
            response=web.HTTPFound('/');response.set_cookie('atlas',s.token,httponly=True,samesite='Strict')
            response.headers['Referrer-Policy']='no-referrer';return response
        elif not hmac.compare_digest(request.cookies.get('atlas',''),s.token):raise web.HTTPUnauthorized(text='Open Thread Atlas with Start.command or python -m atlas.')
        if not bridge and request.method!='GET' and origin and origin!=f'http://127.0.0.1:{s.port}':raise web.HTTPForbidden()
        try:r=await handler(request)
        except (ValueError,TypeError,KeyError) as e:r=web.json_response({'error':str(e)},status=400)
        r.headers['Cache-Control']='no-store';r.headers['X-Content-Type-Options']='nosniff'
        r.headers['Referrer-Policy']='no-referrer'
        r.headers['Content-Security-Policy']="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; object-src 'none'; base-uri 'none'"
        return r
    app=web.Application(middlewares=[guard],client_max_size=128*1024**2)
    async def api(req):
        action=req.match_info['action']
        if req.method=='GET':
            if action=='state':value=s.snapshot(int(req.query['after']) if 'after' in req.query else None)
            elif action=='history':
                seq=req.query.get('seq')
                if seq is not None and not re.fullmatch(r'[1-9][0-9]{0,18}',seq):raise ValueError('Invalid message number')
                value=s.store.history(req.query['id'],seq)
            elif action=='search':value=s.store.search(req.query.get('q',''),req.query.get('provider') or None)
            elif action=='reply':
                seq=req.query.get('seq','')
                if not re.fullmatch(r'[1-9][0-9]{0,18}',seq):raise ValueError('Invalid reply number')
                row=s.store.db.execute("""SELECT m.session,m.seq,m.body,m.at FROM messages m
                    JOIN sessions s ON s.id=m.session WHERE m.seq=? AND m.role='assistant' AND s.hidden=0""",(seq,)).fetchone()
                value=dict(row) if row else None
            elif action=='pairing':value={'token':s.bridge,'port':s.port}
            else:raise web.HTTPNotFound()
        else:
            if req.content_type!='application/json':raise web.HTTPUnsupportedMediaType()
            d=await req.json()
            if action=='edit':s.store.edit(d['id'],d);value={'ok':True}
            elif action=='group':value={'id':s.store.group(d['name'])}
            elif action=='send':value=s.enqueue(d)
            elif action=='import':value={'count':await asyncio.to_thread(import_export,s.store,d['provider'],d['account'],d['data'])}
            elif action=='notifications':s.store.db.execute("INSERT OR REPLACE INTO prefs VALUES('notifications',?)",('1' if d.get('enabled') else '0',));value={'ok':True}
            else:raise web.HTTPNotFound()
        return web.json_response(value)
    async def upload(req):
        import io, zipfile
        raw=await req.read()
        provider=req.query['provider'];account=req.query['account']
        def parse():
            if raw.startswith(b'PK'):
                with zipfile.ZipFile(io.BytesIO(raw)) as z:
                    names=[i for i in z.infolist() if re.fullmatch(r'conversations(?:[-_]?[0-9]+)?\.json',Path(i.filename).name,re.I)]
                    if not names or sum(i.file_size for i in names)>256*1024**2:raise ValueError('Missing or oversized conversations JSON in ZIP')
                    all_chats=[]
                    for i in names:
                        batch=json.loads(z.read(i))
                        if isinstance(batch,dict):batch=batch.get('conversations')
                        if not isinstance(batch,list):raise ValueError('Invalid export file')
                        all_chats.extend(batch)
            else:all_chats=json.loads(raw)
            return import_export(s.store,provider,account,all_chats)
        try:count=await asyncio.to_thread(parse)
        except zipfile.BadZipFile:raise ValueError('Invalid ZIP archive')
        return web.json_response({'count':count})
    async def bridge(req):
        d=await req.json()
        if req.path=='/bridge/disconnect':
            with s.store.tx():
                s.store.db.execute("UPDATE sessions SET bridge='',live_at=0,writable=0 WHERE bridge=? AND source=?",(d.get('tab'),d.get('source')))
            return web.json_response({'ok':True})
        return web.json_response(s.web_sync(d) if req.path=='/bridge/sync' else s.ack(d))
    async def asset(req):
        name=req.match_info.get('asset') or 'index.html'
        if name not in ('index.html','style.css','app.js','router.js'):raise web.HTTPNotFound()
        return web.FileResponse(ROOT/'public'/name)
    async def start(_):
        s.spawn(s.loop());s.spawn(s.capabilities());s.spawn(s.dispatch())
    async def stop(_):
        tasks=list(s.tasks)
        for task in tasks:task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
    app.on_startup.append(start);app.on_cleanup.append(stop)
    app.router.add_route('*','/api/{action}',api)
    app.router.add_post('/import',upload);app.router.add_post('/bridge/sync',bridge);app.router.add_post('/bridge/ack',bridge);app.router.add_post('/bridge/disconnect',bridge)
    app.router.add_get('/launch/{key}',asset);app.router.add_get('/',asset);app.router.add_get('/{asset}',asset)
    return app


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser()
    data_root=(Path.home()/'Library'/'Application Support' if sys.platform=='darwin' else Path(os.environ.get('XDG_DATA_HOME',Path.home()/'.local/share')))
    parser.add_argument('--data',type=Path,default=data_root/'Thread Atlas')
    parser.add_argument('--codex-dir',type=Path,default=Path(os.environ.get('CODEX_HOME',Path.home()/'.codex'))/'sessions')
    parser.add_argument('--claude-dir',type=Path,default=Path(os.environ.get('CLAUDE_CONFIG_DIR',Path.home()/'.claude'))/'projects')
    parser.add_argument('--port',type=int,default=43821)
    parser.add_argument('--no-open',action='store_true');parser.add_argument('--notify',action='store_true')
    args=parser.parse_args()
    try:s=Service(args.data,args.codex_dir,args.claude_dir,args.port,args.notify)
    except BlockingIOError:
        url=(args.data/'launch.url').read_text().strip()
        if not args.no_open:webbrowser.open(url)
        print('Thread Atlas is already running.');return
    url=f'http://127.0.0.1:{s.port}/launch/{s.login}'
    (s.data/'launch.url').write_text(url)
    app=application(s)
    async def opened(_):
        if not args.no_open:asyncio.get_running_loop().call_later(.5,webbrowser.open,url)
    app.on_startup.append(opened)
    web.run_app(app,host='127.0.0.1',port=args.port,access_log=None)
