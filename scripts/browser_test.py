"""Real Chromium extension + local HTTP server, with isolated provider DOM fixtures."""
import io
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
FIXTURE="""<!DOCTYPE html><title>TOPIC</title><main id="history">INITIAL</main>
<textarea data-testid="chat-input" placeholder="Message"></textarea><button data-testid="send-button" aria-label="Send message">Send</button>
<script>
const provider='PROVIDER';
function bubble(role,text){
 const d=document.createElement('div');
 d.dataset.messageId=crypto.randomUUID();
 if(provider==='ChatGPT'){
   if(role==='user'){d.className='bg-user-message';const span=document.createElement('span');span.className='whitespace-pre-wrap';span.textContent=text;d.append(span);}
   else {d.className='DilResponseRoot-fixture';const block=document.createElement('p');block.className='MarkdownRoot-fixture';block.textContent=text;d.append(block);}
 }
 else d.dataset.testid=role+'-message';
 if(provider!=='ChatGPT')d.textContent=text;document.querySelector('main').append(d);
}
document.querySelector('button').onclick=()=>{
 const box=document.querySelector('textarea');bubble('user',box.value);box.value='';
 setTimeout(()=>bubble('assistant','MARKER reply from the original '+provider+' fixture.'),250);
};
</script>"""


def full_history_lookup(page,origin,token):
    """A lookup finds a thread by an older message, shows it with its time, and View source opens the history there."""
    # The live fixture titled 'Payment retry policy' would match by title. Hiding it leaves only the older message to match.
    titled=next(i for i in page.request.get(origin+'/api/state').json()['sessions'] if i['title']=='Payment retry policy')
    assert page.request.post(origin+'/api/edit',data={'id':titled['id'],'hidden':True}).ok
    older='Agreed payment retry policy: retry failed charges three times with exponential backoff.'
    thread=[{'uuid':'billing-0','sender':'human','text':older,'created_at':'2025-03-14T12:00:00Z'}]+[
        {'uuid':'billing-'+str(n),'sender':'assistant' if n%2 else 'human','text':'CSV export step '+str(n)+' is done.',
         'created_at':'2025-03-15T00:%02d:00Z'%n} for n in range(1,41)]
    decoy=[{'uuid':'invoice-1','sender':'assistant','text':'The payment button is now green.','created_at':'2025-03-16T00:00:00Z'}]
    assert page.request.post(origin+'/api/import',data={'provider':'Claude','account':'History','data':[
        {'uuid':'billing-sync','name':'Billing sync notes','chat_messages':thread},
        {'uuid':'invoice-layout','name':'Invoice layout','chat_messages':decoy}]}).ok
    page.locator('[data-view=map]').click();expect(page.locator('.card').filter(has_text='Billing sync notes')).to_have_count(1,timeout=10000)
    page.locator('#prompt').fill('Where did we leave the payment retry policy?');page.locator('#prompt').press('Enter')
    answer=page.locator('#chat .answer').last
    expect(answer).to_contain_text('Billing sync notes');expect(answer).to_contain_text(older)
    when=datetime.fromtimestamp(datetime.fromisoformat('2025-03-14T12:00:00+00:00').timestamp())
    expect(answer).to_contain_text(re.compile(rf'{when:%b} {when.day}, {when.hour%12 or 12}:{when:%M}\s?{when:%p}'))
    link=answer.locator('[data-seq]');expect(link).to_have_count(1);seq=link.get_attribute('data-seq');link.click()
    match=page.locator('#dialog .history section.match')
    expect(match).to_have_attribute('data-seq',seq);expect(match).to_contain_text(older);expect(match).to_be_in_viewport()
    page.locator('#closeDialog').click()


def claude_thread(uuid,name,*messages):
    """One Claude export conversation, a day per message from April 1, 2025."""
    return {'uuid':uuid,'name':name,'chat_messages':[{'uuid':uuid+'-'+str(n),'sender':role,'text':text,'created_at':'2025-04-%02dT12:00:00Z'%(n+1)}
                                                     for n,(role,text) in enumerate(messages)]}


def show(page,origin,*threads):
    assert page.request.post(origin+'/api/import',data={'provider':'Claude','account':'History','data':list(threads)}).ok
    page.locator('[data-view=map]').click()
    for t in threads:expect(page.locator('.card').filter(has_text=t['name'])).to_have_count(1,timeout=10000)


def ask(page,text):
    """Submit a request and return the dashboard's answer to it."""
    page.locator('#prompt').fill(text);page.locator('#prompt').press('Enter')
    return answer_to(page,text)


def answer_to(page,text):
    answer=page.locator('#chat .utter').filter(has_text=text).last.locator('xpath=following-sibling::div[contains(@class,"answer")][1]')
    expect(answer).to_have_count(1);return answer


def older_match_stays_unread(page,origin,token):
    """Opening the history at an older matched message leaves the thread's newer reply unread."""
    older='Agreed tax nexus: register in the three new states by June.'
    sid=page.request.post(origin+'/bridge/sync',headers={'Authorization':'Bearer '+token},data={
        'provider':'Claude','account':'Personal','source':'state-filings','tab':'tab-state-filings-0001','title':'State filings',
        'messages':[{'id':'filing-1','role':'user','text':older,'at':'2025-04-01T12:00:00Z'},
                    {'id':'filing-2','role':'assistant','text':'Drafted the cover letter.'}]}).json()['session']
    unread=lambda:next(i for i in page.request.get(origin+'/api/state').json()['sessions'] if i['id']==sid)['unread']
    assert unread()
    page.locator('[data-view=map]').click();expect(page.locator('.card').filter(has_text='State filings')).to_have_count(1,timeout=10000)
    answer=ask(page,'Where did we leave the tax nexus?');expect(answer).to_contain_text(older)
    answer.locator('[data-seq]').click();expect(page.locator('#dialog .history section.match')).to_contain_text(older)
    page.locator('#closeDialog').click()
    assert unread()


def far_apart_terms_route(page,origin,token):
    """Routing counts every query word in the matched message, not only the words in its excerpt."""
    filler=' '.join(['and then we compared every number in the quarterly sheet with the bank export line by line']*4)
    show(page,origin,claude_thread('month-end','Month-end notes',('human','The ledger '+filler+' cutoff is the 25th.'),('assistant','Archived.')),
         claude_thread('release-calendar','Release calendar',('human','The cutoff for the release moved to Monday.'),('assistant','Updated.')))
    answer=ask(page,'Where did we leave the ledger cutoff?')
    expect(answer).to_contain_text('Month-end notes');expect(answer).not_to_contain_text('Which thread')


def latest_reply_identity(page,origin,token):
    """The matched message is shown apart from the latest reply exactly when it is a different message."""
    older='Webhook timeout is now 30 seconds.'
    filler=' '.join('Paragraph %d lists the folders we moved.'%n for n in range(80))
    show(page,origin,claude_thread('webhooks','Webhook settings',('human','What should the limit be?'),('assistant',older),('human','Anything else?'),
                                   ('assistant','Recap: '+older+' I also renamed the queue workers and moved the on-call dashboards.')),
         claude_thread('archive','Archive notes',('human','Write up the notes.'),('assistant',filler+' The archive retention window is ninety days.')))
    # An older reply whose text the latest reply repeats is still shown, with its own time and link.
    answer=ask(page,'Where did we leave the webhook timeout?')
    expect(answer.locator('.quote')).to_have_count(2);expect(answer.locator('.quote').first).to_contain_text('Matched message')
    expect(answer.locator('.quote').first).to_contain_text(older);expect(answer.locator('.quote').first.locator('[data-seq]')).to_have_count(1)
    # A latest reply that matched past the stored cut is shown once, as the matched message with its link.
    answer=ask(page,'Where did we leave the archive retention window?')
    expect(answer).to_contain_text('Archive notes');expect(answer.locator('.quote')).to_have_count(1)
    expect(answer.locator('.quote')).to_contain_text('ninety days');answer.locator('[data-seq]').click()
    expect(page.locator('#dialog .history section.match')).to_contain_text('The archive retention window is ninety days.')
    page.locator('#closeDialog').click()


def older_match_choices(page,origin,token):
    """Threads matched only by older messages are offered as choices, never sent to unasked."""
    cadence='Dunning cadence: reminders go out every three days.'
    show(page,origin,claude_thread('accounts','Accounts follow-up',('human',cadence),('assistant','Sent the summary.')),
         claude_thread('customer-mail','Customer emails',('human',cadence),('assistant','Fixed the footer.')),
         claude_thread('payouts','Vendor payouts',('human','The escheatment threshold is 300 dollars.'),('assistant','Booked.')))
    page.locator('#prompt').fill('Change the dunning cadence to weekly reminders');expect(page.locator('#dest')).to_contain_text('Choose a destination')
    page.locator('#prompt').press('Enter');answer=answer_to(page,'Change the dunning cadence to weekly reminders')
    expect(answer).to_contain_text('Which thread did you mean?');expect(answer.locator('.choice')).to_have_count(2)
    expect(answer.locator('.choice').filter(has_text='Accounts follow-up')).to_have_count(1);expect(answer.locator('.choice').filter(has_text='Customer emails')).to_have_count(1)
    page.locator('#prompt').fill('Change the escheatment threshold to 500 dollars');expect(page.locator('#dest')).to_contain_text('Confirm before sending · ')
    expect(page.locator('#dest')).to_contain_text('Vendor payouts')
    page.locator('#prompt').press('Enter');answer=answer_to(page,'Change the escheatment threshold to 500 dollars')
    expect(answer).to_contain_text('Which thread did you mean?');expect(answer.locator('.choice')).to_have_count(1)
    expect(answer.locator('.choice')).to_contain_text('Vendor payouts')


def provider_scoped_lookup(page,origin,token):
    """A request that names a provider searches that provider's history, so stronger matches elsewhere cannot crowd it out."""
    def chatgpt(n):
        node=lambda key,role,text,parent:{'message':{'id':key,'author':{'role':role},'content':{'parts':[text]},'create_time':1743500000+n},'parent':parent}
        return {'conversation_id':'vendor-note-'+str(n),'title':'Vendor note '+str(n),'current_node':'b',
                'mapping':{'a':node('a','user','Escrow audit, escrow audit.',None),'b':node('b','assistant','Escrow audit done.','a')}}
    assert page.request.post(origin+'/api/import',data={'provider':'ChatGPT','account':'Vendors','data':[chatgpt(n) for n in range(21)]}).ok
    older=('Before the quarter closes we still need to finish the escrow audit, which covers the vendor deposit accounts, '
           'the clearing balances against the bank statements, and the sign-off from finance.')
    show(page,origin,claude_thread('quarterly-close','Quarterly close',('human',older),('assistant','Booked the meeting.')))
    answer=ask(page,'Where did Claude leave the escrow audit?')
    expect(answer).to_contain_text('Quarterly close');expect(answer).to_contain_text(older)


def live_thread(page,origin,token,source,title,*messages):
    """A connected, writable Claude tab, so the dashboard can send to it directly."""
    return page.request.post(origin+'/bridge/sync',headers={'Authorization':'Bearer '+token},data={
        'provider':'Claude','account':'Personal','source':source,'tab':'tab-'+source+'-0001','title':title,'writable':True,
        'messages':[{'id':source+'-'+str(n),'role':role,'text':text} for n,(role,text) in enumerate(messages)]}).json()['session']


def sent(page,origin,sid):
    """How many messages the dashboard has sent to a session."""
    return sum(d['session']==sid for d in page.request.get(origin+'/api/state').json()['deliveries'])


def pending_lookup_keeps_target(page,origin,token):
    """Pinning another thread while a lookup is still searching does not turn the lookup into a send there."""
    older='Agreed vendor escalation matrix: page the account owner first.'
    show(page,origin,claude_thread('vendor-escalation','Vendor contacts',('human',older),('assistant','Saved the contact sheet.')))
    sid=live_thread(page,origin,token,'standup-notes','Standup notes',('user','Collect the notes.'),('assistant','Collected the notes.'))
    expect(page.locator('.card').filter(has_text='Standup notes')).to_have_count(1,timeout=10000)
    held=[]
    page.route('**/api/search*',lambda r:held.append(r))
    question='Where did we leave the vendor escalation matrix?'
    page.locator('#prompt').fill(question);page.locator('#prompt').press('Enter')
    page.locator('.card').filter(has_text='Standup notes').click();page.locator('#dialog [data-pick]').click()
    expect(page.locator('#mode')).to_have_value('send')
    # The dashboard searches before it answers. Wait up to 5 s for that search to be held, then let it through.
    for _ in range(100):
        if held:break
        page.wait_for_timeout(50)
    for r in held:r.continue_()
    page.unroute('**/api/search*')
    answer=answer_to(page,question)
    expect(answer).to_contain_text('Vendor contacts');expect(answer).to_contain_text(older)
    assert sent(page,origin,sid)==0
    page.locator('[data-clear]').click()


def long_draft_lookup_and_send(page,origin,token):
    """Drafts longer than a URL allows still find a thread by an older message, and still send to a pinned thread."""
    older='Agreed chargeback evidence checklist: receipts, shipping proof and the customer emails.'
    show(page,origin,claude_thread('chargebacks','Dispute handling',('human',older),('assistant','Filed the template.')))
    question='Where did we leave the chargeback evidence checklist? '+'x'*9000
    page.locator('#prompt').fill(question);page.locator('#prompt').press('Enter')
    answer=page.locator('#chat .utter').last.locator('xpath=following-sibling::div[contains(@class,"answer")][1]')
    expect(answer).to_contain_text('Dispute handling');expect(answer).to_contain_text(older)
    sid=live_thread(page,origin,token,'release-notes','Release notes',('user','Draft the notes.'),('assistant','Drafted the notes.'))
    card=page.locator('.card').filter(has_text='Release notes');expect(card).to_have_count(1,timeout=10000)
    card.click();page.locator('#dialog [data-pick]').click()
    draft='Please apply this patch. '+'y'*9000
    page.locator('#prompt').fill(draft);page.locator('#prompt').press('Enter')
    expect(page.locator('#chat .answer').last).to_contain_text('Release notes')
    assert sent(page,origin,sid)==1
    page.locator('[data-clear]').click()


STEPS=[full_history_lookup,older_match_stays_unread,far_apart_terms_route,latest_reply_identity,older_match_choices,
       provider_scoped_lookup,pending_lookup_keeps_target,long_draft_lookup_and_send]


def main():
    # Step names given as arguments run only those full-history steps, after the shared checks.
    steps=[next(f for f in STEPS if f.__name__==name) for name in sys.argv[1:]] or STEPS
    with tempfile.TemporaryDirectory() as tmp,sync_playwright() as p:
        d=Path(tmp);ext=ROOT/'extension'
        with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
        origin=f'http://127.0.0.1:{port}'
        cmd=[sys.executable,'-m','atlas','--data',str(d/'data'),'--codex-dir',str(d/'none1'),'--claude-dir',str(d/'none2'),'--no-open','--port',str(port)]
        proc=subprocess.Popen(cmd,cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            ctx=p.chromium.launch_persistent_context(str(d/'browser'),channel='chromium',headless=True,locale='ko-KR',viewport={'width':1400,'height':900},
                args=[f'--disable-extensions-except={ext}',f'--load-extension={ext}'])
            worker=ctx.service_workers[0] if ctx.service_workers else ctx.wait_for_event('serviceworker')
            page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
            for _ in range(100):
                if (d/'data/launch.url').exists():break
                time.sleep(.1)
            page.goto((d/'data/launch.url').read_text());expect(page.locator('h1')).to_have_text('Work map')
            token=page.evaluate("fetch('/api/pairing').then(r=>r.json()).then(d=>d.token)")
            assert p.request.new_context().get(origin+'/api/state').status==401
            assert page.request.post(origin+'/api/group',data={'name':'Blocked'},headers={'Origin':'https://invalid.example'}).status==403
            assert page.request.post(origin+'/bridge/sync',data={}).status==401
            pages=[]
            for provider,src,topic,marker in [('ChatGPT','atlas-sample-1','Authentication review','ALPHA'),('Claude','atlas-sample-2','Payment retry policy','BETA')]:
                source=ctx.new_page()
                url=('https://chatgpt.com/c/' if provider=='ChatGPT' else 'https://claude.ai/chat/')+src
                initial=('<div data-message-author-role="assistant" data-message-id="initial">Original authentication question?</div>'
                         if provider=='ChatGPT' else '<div data-testid="assistant-message" data-message-id="initial">Original payment question?</div>')
                fixture=FIXTURE.replace('INITIAL',initial).replace('PROVIDER',provider).replace('MARKER',marker).replace('TOPIC',topic)
                source.route(url,(lambda html: lambda r:r.fulfill(body=html,content_type='text/html'))(fixture))
                source.goto(url);pages.append(source)
                # Pair the test tab through the service worker using the same binding and messages as popup.js.
                result=worker.evaluate("""async ({url,pairing})=>{
                  const [tab]=await chrome.tabs.query({url});const identity=await chrome.tabs.sendMessage(tab.id,{type:'identity'});
                  await chrome.storage.session.set({['tab'+tab.id]:{...identity,account:'Personal',pairing,allowed:true,tab:crypto.randomUUID()}});
                  return chrome.tabs.sendMessage(tab.id,{type:'activate'});
                }""",{'url':url,'pairing':str(port)+':'+token})
                assert not result.get('error'),result
            page.bring_to_front()
            expect(page.locator('.card')).to_have_count(2,timeout=12000)
            for idx,(topic,marker) in enumerate([('Authentication review','ALPHA'),('Payment retry policy','BETA')]):
                box=page.locator('#prompt');box.fill('Send a follow-up about '+topic)
                expect(page.locator('#dest')).to_contain_text(topic)
                box.press('Enter')
                expect(pages[idx].locator('main')).to_contain_text(marker+' reply',timeout=15000)
                expect(page.locator('#chat')).to_contain_text('answered',timeout=15000)
                expect(page.locator('.card').filter(has_text=topic)).to_contain_text(marker+' reply',timeout=15000)
                assert pages[idx].locator('main').inner_text().count('Send a follow-up')==1
            info=page.evaluate("fetch('/api/state').then(r=>r.json())")
            assert len(info['deliveries'])==2 and all(x['status']=='answered' for x in info['deliveries'])
            # ZIP import goes through the actual settings dialog and file picker.
            archive=d/'export.zip'
            with zipfile.ZipFile(archive,'w') as z:z.writestr('conversations.json',json.dumps([{'uuid':'fixture-import','name':'Refund guidelines','chat_messages':[{'uuid':'reply-import','sender':'assistant','text':'A refund takes five business days.','created_at':'2025-01-01T00:00:00Z'}]}]))
            page.locator('#connections').click();page.locator('#provider').select_option('Claude');page.locator('#file').set_input_files(str(archive))
            expect(page.locator('#importResult')).to_contain_text('Imported 1')
            page.locator('#closeDialog').click()
            expect(page.locator('.card')).to_have_count(3)
            page.locator('#prompt').fill('Change the refund guidelines')
            expect(page.locator('#dest')).to_contain_text('Read-only')
            page.locator('#prompt').press('Enter');expect(page.locator('#chat')).to_contain_text('Copy draft')
            card=page.locator('.card').filter(has_text='Refund guidelines')
            card.drag_to(page.locator('#hiddenDrop'));expect(page.locator('.card')).to_have_count(2)
            page.reload();expect(page.locator('#hiddenCount')).to_have_text('1')
            page.locator('#hiddenDrop').click();page.locator('[data-restore]').click()
            page.locator('[data-view=map]').click();expect(page.locator('.card')).to_have_count(3)
            page.once('dialog',lambda dialog:dialog.accept('Release prep'))
            page.locator('#addGroup').click()
            root=page.locator('.root').filter(has_text='Release prep')
            card.drag_to(root);expect(page.locator('.group').filter(has=root).locator('.card')).to_contain_text('Refund guidelines')
            page.locator('.card').filter(has_text='Authentication review').drag_to(page.locator('#composer'))
            page.locator('#prompt').fill('Draft to keep')
            page.locator('[data-lang=ko]').click();expect(page.locator('html')).to_have_attribute('lang','ko')
            expect(page.locator('#prompt')).to_have_value('Draft to keep')
            page.locator('[data-lang=en]').click();expect(page.locator('#dest')).to_contain_text('Authentication review')
            page.locator('#prompt').fill('');page.locator('#mode').select_option('auto')
            page.locator('#prompt').fill('Where did we leave authentication review?');page.locator('#prompt').press('Enter')
            expect(page.locator('#chat .answer').last).to_contain_text('ALPHA')
            # A native notification identifies the exact reply, even if a newer answer arrived before the click.
            notification=next(m for m in info['inbox'] if 'ALPHA reply' in m['body'])
            opened=ctx.new_page()
            opened.goto((d/'data/launch.url').read_text().strip()+'#reply-'+str(notification['seq']))
            expect(opened.locator('h1')).to_have_text('Inbox')
            expect(opened.locator('#dialogContent')).to_contain_text(notification['body'])
            expect(opened.locator('#dialogTitle')).to_have_text('Authentication review')
            opened.locator('#closeDialog').click();opened.close()
            if os.environ.get('ATLAS_SCREENSHOT'):page.screenshot(path=os.environ['ATLAS_SCREENSHOT'])
            for width in (390,820,1024,1440):
                page.set_viewport_size({'width':width,'height':900})
                assert page.evaluate('document.documentElement.scrollWidth')<=width
            batch=[{'uuid':'scale-'+str(n),'name':'Scale case '+str(n),'chat_messages':[
                    {'uuid':'m-'+str(n),'sender':'assistant','text':'Fixture '+str(n)}]} for n in range(100)]
            imported=page.request.post(origin+'/api/import',data={'provider':'Claude','account':'Scale','data':batch})
            assert imported.ok
            expect(page.locator('.card')).to_have_count(103,timeout=10000)
            page.locator('#viewAction').click();expect(page.locator('.card')).to_have_count(0)
            page.locator('#viewAction').click();expect(page.locator('.card')).to_have_count(103)
            # Notifications keep working after their replies fall out of the 150-entry inbox window.
            extra=[{'uuid':'old-window-'+str(n),'chat_messages':[{'uuid':'m','sender':'assistant','text':'Later','created_at':'2090-01-01T00:00:00Z'}]} for n in range(160)]
            assert page.request.post(origin+'/api/import',data={'provider':'Claude','account':'Window','data':extra}).ok
            assert notification['seq'] not in [m['seq'] for m in page.request.get(origin+'/api/state').json()['inbox']]
            old_url=(d/'data/launch.url').read_text().strip()+'#reply-'+str(notification['seq'])
            page.goto(old_url);expect(page.locator('#dialogContent')).to_contain_text(notification['body'])
            assert page.request.post(origin+'/api/edit',data={'id':notification['session'],'hidden':True}).ok
            page.reload();expect(page.locator('h1')).to_have_text('Inbox');expect(page.locator('#dialog')).not_to_be_visible()
            for step in steps:step(page,origin,token)
            assert not errors,errors
            ctx.close();print('PASS: both extension DOM round trips, badge receipts, read-only ZIP import, lookup, routing, drag, hide/restore, reload, language switch, authorization, 4 widths, a 103-session map, and full-history lookups and routing.')
        finally:proc.terminate();proc.wait(timeout=20)


if __name__=='__main__':main()
