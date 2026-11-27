import {named,route} from './router.js';
const $=id=>document.getElementById(id);
const escape=value=>String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const load=(key,def)=>{try{return JSON.parse(localStorage.getItem('atlas:'+key))??def;}catch{return def;}};
const save=(key,obj)=>localStorage.setItem('atlas:'+key,JSON.stringify(obj));
let lang=load('lang','en'),view='map',filter=null,focus=load('target',null),locked=load('locked',false);
let folded=new Set(load('folded',[])),messages=load('chat',[]),state={sessions:[],groups:[],inbox:[],deliveries:[]};
let received=null,ready=false,polling=false,requestSeq=0,appliedSeq=0,dragging=false,lastPaint='',timer,searchTimer,draftID=load('draftID',null);
let found={q:null,hits:[]};
const l=(en,ko)=>lang==='ko'?ko:en;
const labels={map:['Work map','작업 지도'],inbox:['Inbox','받은 답변'],hidden:['Hidden','숨김'],projects:['Projects','프로젝트'],connections:['Connections','연결 범위'],newGroup:['New project','새 프로젝트'],workspace:['Workspace','워크스페이스'],myWork:['My work','내 작업'],mainChat:['Main chat','메인 대화'],message:['Message','메시지'],send:['Send','보내기'],close:['Close','닫기']};
const text=key=>l(...labels[key]);
const item=id=>state.sessions.find(i=>i.id===id);
const name=i=>i?.title||l('Untitled session','제목 없는 세션');
const project=id=>state.groups.find(g=>g.id===id)?.name||l('Unsorted','미분류');
const path=i=>project(i.group_id)+' › '+name(i);
const stamp=n=>n?new Date(n*1000).toLocaleString(lang==='ko'?'ko-KR':'en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'}):'—';
const active=()=>state.sessions.filter(i=>!i.hidden);
const status=i=>i.owned_terminal?l('Open in terminal','다른 터미널에서 실행 중'):i.state==='run'?l('Running','실행 중'):i.state==='wait'?l('Needs reply','답변 대기'):i.direct?l('Connected','연결됨'):l('Read-only','읽기 전용');
function toast(msg){$('toast').textContent=msg;clearTimeout(timer);timer=setTimeout(()=>$('toast').textContent='',4200);}
async function api(action,data){
 const res=await fetch('/api/'+action,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
 if(!res.ok){const err=await res.json().catch(()=>({}));throw Error(err.error||'Request failed ('+res.status+')');}
 return res.json();
}
function dialog(title,html){$('dialogTitle').textContent=title;$('dialogContent').innerHTML=html;if(!$('dialog').open)$('dialog').showModal();}
function applyLanguage(){
 document.documentElement.lang=lang;document.querySelectorAll('[data-label]').forEach(e=>e.textContent=text(e.dataset.label));
 document.querySelectorAll('[data-lang]').forEach(e=>e.setAttribute('aria-pressed',e.dataset.lang===lang));
 $('mode').options[0].textContent=l('Auto','자동');$('mode').options[1].textContent=l('Find','찾기');$('mode').options[2].textContent=l('Send to thread','스레드에 전송');
 $('prompt').placeholder=l('Find a thread or give it a follow-up…','하던 일을 찾거나 다음 요청을 입력하세요');
 $('chatPanel').ariaLabel=text('mainChat');$('canvas').ariaLabel=text('map');$('activeScope').textContent=l('Local workspace','로컬 워크스페이스');
 redraw();
}
function card(i){
 return '<div class="leaf"><button draggable="true" class="card '+(focus===i.id?'focus':'')+'" data-card="'+i.id+'"><span class="ctop"><span class="provider">'+escape(i.provider)+' · '+escape(i.account)+'</span><span class="state '+(i.state==='wait'?'wait':'')+'">'+escape(status(i))+'</span></span><span class="ctitle">'+escape(name(i))+(focus===i.id?'<span class="target">↳ '+l('Reply target','대화 대상')+'</span>':'')+'</span><span class="recent" style="display:block">'+escape(i.latest||l('No assistant reply yet','아직 답변 없음'))+'</span><span class="sub">'+stamp(i.latest_at)+'</span></button></div>';
}
function redraw(){
 const sessions=active(),groups=state.groups.filter(g=>!filter||filter===g.id);
 $('mapCount').textContent=sessions.length;$('hiddenCount').textContent=state.sessions.length-sessions.length;$('inboxCount').textContent=sessions.filter(i=>i.unread).length;
 $('projectList').innerHTML=state.groups.map(g=>'<button class="projectButton '+(g.id===filter?'active':'')+'" data-project="'+g.id+'"><span class="projectdot" style="--tone:#688779"></span>'+escape(g.name)+'<small>'+sessions.filter(i=>i.group_id===g.id).length+'</small></button>').join('');
 document.querySelectorAll('[data-view]').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
 $('heading').textContent=view==='map'&&filter?project(filter):text(view);
 const area=$('canvas');area.className=view==='map'?'board':'';$('viewAction').hidden=view!=='map'||!groups.length;
 $('viewAction').textContent=groups.every(g=>folded.has(g.id))?l('Expand all','모두 펼치기'):l('Collapse all','모두 접기');
 if(view==='map'){
  $('summary').textContent=l('Projects','프로젝트')+' '+groups.length+' · '+l('Sessions','세션')+' '+sessions.filter(i=>!filter||filter===i.group_id).length;
  area.innerHTML=groups.map(g=>{const children=sessions.filter(i=>i.group_id===g.id);
   const branch=folded.has(g.id)?'<span class="sub">'+children.length+' · '+children.filter(i=>i.unread).length+' '+l('unread','미확인')+'</span>':children.map(card).join('')||'<span class="sub">'+l('Drop a session here','세션을 여기에 놓으세요')+'</span>';
   return '<div class="group" data-group="'+g.id+'"><button class="root" data-fold="'+g.id+'" aria-expanded="'+!folded.has(g.id)+'">'+escape(g.name)+' <small>'+(folded.has(g.id)?'▸':'▾')+'</small></button><div class="branch">'+branch+'</div></div>';
  }).join('')||'<p class="empty">'+l('Looking for local sessions. Open Connections to import web conversations or pair a browser.','로컬 세션을 찾는 중입니다. 연결 범위에서 웹 대화를 가져오거나 브라우저를 연결하세요.')+'</p>';
  $('hint').textContent=l('Drop a card on a project, on Hidden, or on the message box.','카드를 프로젝트, 숨김 또는 입력창에 놓으세요.');
 }else if(view==='inbox'){
  $('summary').textContent=l('Latest replies from visible sessions','표시 중인 세션의 최신 답변');
  area.innerHTML=state.inbox.map(m=>{const i=item(m.session);return i?'<article class="listitem"><span class="sub">'+(m.unread?'● ':'')+escape(i.provider)+' · '+stamp(m.at)+'</span><h3>'+escape(name(i))+'</h3><p>'+escape(m.body.slice(0,500))+'</p><div class="actions"><button class="choice" data-card="'+i.id+'">'+l('View source','원문 보기')+'</button><button class="choice" data-pick="'+i.id+'">'+l('Reply here','이어서 답장')+'</button></div></article>':'';}).join('')||'<p class="empty">'+l('No replies yet.','받은 답변이 없습니다.')+'</p>';
  $('hint').textContent='';
 }else{
  $('summary').textContent=l('Hidden from the map, alerts, and routing','지도, 알림, 라우팅에서 제외됨');
  area.innerHTML=state.sessions.filter(i=>i.hidden).map(i=>'<article class="listitem"><h3>'+escape(name(i))+'</h3><button class="choice" data-restore="'+i.id+'">'+l('Restore','복구')+'</button></article>').join('')||'<p class="empty">'+l('No hidden sessions.','숨긴 세션이 없습니다.')+'</p>';$('hint').textContent='';
 }
 $('statusline').textContent=(state.scan==='Ready'?l('Synced locally','로컬 동기화됨'):state.scan||l('Connecting…','연결 중…'))+' · '+stamp(state.lastScan);
 renderChat();preview();
}
// `at` is the reply target and mode a request was made with, so a later change cannot redirect it.
function plan(text,hits,at={focus,mode:$('mode').value,locked}){return route(text,state.sessions,at.focus,at.mode,at.locked,hits);}
// Only the start of a long draft is searched, which keeps the request within the server's URL limit.
async function search(q){const provider=named(q),hits=await api('search?q='+encodeURIComponent(q.slice(0,500))+(provider?'&provider='+encodeURIComponent(provider):''));if(q===$('prompt').value.trim())found={q,hits};return hits;}
function preview(){
 const value=$('prompt').value.trim(),target=item(focus);
 if(!value&&target&&!target.hidden){$('dest').innerHTML='↳ '+escape(path(target))+' · '+escape(target.provider)+' <button type="button" data-clear>'+l('Clear','해제')+'</button>';return;}
 const p=value?plan(value,found.q===value?found.hits:[]):null;
 $('dest').textContent=!p?l('Destination · Match from message','보낼 곳 · 메시지에서 찾기'):!p.action?l('Search local history','내 기록에서 찾기'):p.candidates.length!==1?l('Choose a destination','대상 선택'):p.confirm?l('Confirm before sending · ','전송 전 확인 · ')+path(p.candidates[0])+' · '+p.candidates[0].provider:(p.candidates[0].direct?l('To · ','보낼 곳 · '):l('Read-only · ','읽기 전용 · '))+path(p.candidates[0])+' · '+p.candidates[0].provider;
}
function pin(id,explicit){const i=item(id);if(!i||i.hidden)return;focus=id;locked=explicit;save('target',focus);save('locked',locked);folded.delete(i.group_id);save('folded',[...folded]);view='map';filter=null;redraw();}
function push(m){messages.push(m);messages=messages.slice(-70);save('chat',messages);renderChat();}
function renderChat(){
 const box=$('chat'),bottom=box.scrollHeight-box.clientHeight-box.scrollTop<55;
 const html=messages.map((m,n)=>{
  if(m.kind==='user')return '<div class="utter">'+escape(m.body)+'</div>';
  let h='';
  const quote=(label,at,body,seq)=>'<div class="quote"><span class="sub">'+label+' · '+stamp(at)+'</span><p>'+escape(body)+'</p><button class="linkbtn" data-card="'+m.session+'"'+(seq?' data-seq="'+seq+'"':'')+'>'+l('View source','원문 보기')+' ↗</button></div>';
  if(m.kind==='source')h='<b>'+escape(m.path)+'</b>'+(m.match?quote(escape(m.provider)+' · '+l('Matched message','일치한 메시지'),m.match.at,m.match.snippet,m.match.seq):'')+(m.match?.latest?'':quote(escape(m.provider),m.at,m.body));
  if(m.kind==='choose')h=l('Which thread did you mean?','어느 스레드인가요?')+'<div class="replyopts">'+m.ids.map(id=>{const i=item(id);return i&&!i.hidden?'<button '+(m.chosen?'disabled':'')+' class="choice" data-select="'+id+'" data-n="'+n+'"><b>'+escape(path(i))+'</b> · '+escape(i.provider)+'<br>'+escape(i.latest.slice(0,125))+'</button>':'';}).join('')+'</div>';
  if(m.kind==='delivery'){const d=state.deliveries.find(d=>d.id===m.key);h='<b>'+escape(m.path)+'</b><div class="quote"><span>'+escape(d?.status||m.status||'queued')+'</span><p>'+escape(d?.detail&&['failed','uncertain'].includes(d.status)?d.detail:m.body)+'</p></div>';}
  if(m.kind==='readonly')h='<b>'+escape(m.path)+'</b><p>'+l('Read-only. Copy the draft to the original conversation.','읽기 전용입니다. 원본 대화로 초안을 복사하세요.')+'</p><button class="choice" data-copy="'+n+'">'+l('Copy draft','초안 복사')+'</button> <button class="choice" data-card="'+m.session+'">'+l('View source','원문 보기')+'</button>';
  if(m.kind==='info')h=escape(m.body);
  return '<div class="answer"><div class="assistantname">Thread Atlas</div>'+h+'</div>';
 }).join('');
 if(box.innerHTML!==html){box.innerHTML=html;if(bottom)box.scrollTop=box.scrollHeight;}
}
async function refresh(force=false){
 if(polling&&!force)return;polling=true;
 try{
  const req=++requestSeq,after=received;const s=await api('state'+(after===null?'':'?after='+after));
  if(req<appliedSeq)return;appliedSeq=req;
  for(const i of s.sessions){i.project=s.groups.find(g=>g.id===i.group_id)?.name||'';if(i.state==='idle'&&i.latest.trim().endsWith('?'))i.state='wait';}
  for(const m of s.events||[]){
   if(received!==null&&m.seq<=received)continue;
   const i=s.sessions.find(x=>x.id===m.session);
   if(i){toast(l('New reply · ','새 답변 · ')+name(i));if(messages.some(v=>v.kind==='delivery'&&v.session===i.id)&&!messages.some(v=>v.event===m.seq))
    push({kind:'source',event:m.seq,session:i.id,path:i.project+' › '+name(i),provider:i.provider,at:m.at,body:m.body});}
  }
  received=s.cursor;ready=true;state=s;
  if(focus&&(!item(focus)||item(focus).hidden)){focus=null;locked=false;}
  const signature=JSON.stringify([s.scan,s.codex,s.claude,s.groups,s.inbox,s.deliveries,s.sessions.map(({live_at,synced_at,...i})=>i)]);
  if(!dragging&&(force||signature!==lastPaint)){lastPaint=signature;redraw();}

 }catch(e){$('statusline').textContent=l('Disconnected · ','연결 끊김 · ')+e.message;}finally{polling=false;}
}
async function detail(id,seq){
 const i=item(id);if(!i)return;const history=await api('history?id='+encodeURIComponent(id)+(seq?'&seq='+seq:''));
 // A window around an older message leaves newer replies unseen.
 if(!seq)await api('edit',{id,read:true});
 dialog(name(i),'<p class="sub">'+escape(i.provider)+' · '+escape(i.account)+' · '+escape(path(i))+'</p><p>'+escape(status(i))+' · '+l('Last seen ','마지막 확인 ')+stamp(i.synced_at)+'</p><div class="actions"><button class="choice" data-pick="'+i.id+'">'+l('Reply here','이어서 답장')+'</button><button class="choice" data-hide="'+i.id+'">'+l('Hide','숨김')+'</button>'+(i.url?'<a class="choice" target="_blank" rel="noopener noreferrer" href="'+escape(i.url)+'">'+l('Open original','원본 열기')+'</a>':'')+'</div><label class="modalfield">'+l('Move to project','프로젝트 이동')+'<select id="moveSelect">'+state.groups.map(g=>'<option value="'+g.id+'" '+(g.id===i.group_id?'selected':'')+'>'+escape(g.name)+'</option>').join('')+'</select></label><div class="history">'+history.map(m=>'<section data-seq="'+m.seq+'"'+(String(m.seq)===seq?' class="match"':'')+'><strong>'+escape(m.role)+'</strong> <small>'+stamp(m.at)+'</small><p>'+escape(m.body)+'</p></section>').join('')+'</div>');
 $('moveSelect').onchange=async e=>{try{await edit(id,{group:e.target.value});$('dialog').close();}catch(e){toast(e.message);}};const hist=$('dialogContent').querySelector('.history'),match=hist.querySelector('.match');if(match)match.scrollIntoView({block:'center'});else hist.scrollTop=hist.scrollHeight;await refresh(true);
}
async function edit(id,changes){await api('edit',{id,...changes});await refresh(true);}
async function handle(i,body,action,key,match){
 if(!i||i.hidden)throw Error(l('Session unavailable','세션을 사용할 수 없습니다'));
 if(!action){pin(i.id,false);push({kind:'source',session:i.id,path:path(i),provider:i.provider,at:i.latest_at,body:i.latest||l('No assistant reply yet.','아직 답변이 없습니다.'),match});return;}
 if(!i.direct){push({kind:'readonly',path:path(i),session:i.id,body});return;}
 const d=await api('send',{id:key,session:i.id,body});pin(i.id,false);push({kind:'delivery',session:i.id,key,body,path:path(i),status:d.status});await refresh(true);
}
$('composer').onsubmit=async e=>{
 e.preventDefault();const body=$('prompt').value.trim();if(!body)return;
 const key=draftID||crypto.randomUUID();draftID=key;save('draftID',key);
 const submit=$('composer').querySelector('[type=submit]');if(submit.disabled)return;submit.disabled=true;
 try{const at={focus,mode:$('mode').value,locked},pinned=plan(body,[],at);
  const hits=at.locked&&pinned.action&&pinned.candidates[0]?.id===at.focus?[]:await search(body),p=plan(body,hits,at),match=id=>hits.find(f=>f.session===id);
  if(!messages.some(m=>m.kind==='user'&&m.key===key))push({kind:'user',body,key});
  if(!p.candidates.length)push({kind:'info',body:l('No matching visible session.','일치하는 세션이 없습니다.')});
  else if(p.candidates.length===1&&!p.waiting&&!p.confirm)await handle(p.candidates[0],body,p.action,key,match(p.candidates[0].id));
  else push({kind:'choose',ids:p.candidates.map(i=>i.id),body,action:p.action,key,found:p.candidates.map(i=>match(i.id)).filter(Boolean)});
  if($('prompt').value.trim()===body){$('prompt').value='';save('draft','');draftID=null;save('draftID',null);}preview();
 }catch(err){toast(err.message);}finally{submit.disabled=false;}
};
async function settings(){
 const info=await api('pairing');
 const counts=['Codex','Claude Code','ChatGPT','Claude'].map(p=>{const list=state.sessions.filter(i=>i.provider===p);return '<p><b>'+p+'</b> · '+list.length+' '+l('indexed','색인됨')+' · '+list.filter(i=>i.direct).length+' '+l('writable','전송 가능')+'</p>';}).join('');
 dialog(text('connections'),counts+'<label class="modalfield"><span>'+l('Local notifications','로컬 알림')+'</span><input id="alerts" type="checkbox" '+(state.notifications?'checked':'')+'></label><hr><p><b>'+l('Import conversation history','대화 기록 가져오기')+'</b></p><label class="modalfield">'+l('Service','서비스')+'<select id="provider"><option>ChatGPT</option><option>Claude</option></select></label><label class="modalfield">'+l('Account label','계정 이름')+'<input id="account" value="Personal"></label><label class="modalfield">'+l('Export file (.json or .zip)','내보내기 파일 (.json 또는 .zip)')+'<input id="file" type="file" accept=".json,.zip"></label><p id="importResult" role="status"></p><hr><p><b>'+l('Connect a browser tab','브라우저 탭 연결')+'</b></p><p class="sub">'+l('Load the extension folder in Chrome, open a source conversation, then paste the pairing key into the extension. Use the same account label when importing.','Chrome에 extension 폴더를 로드한 뒤 원본 대화에서 확장 프로그램에 연결 키를 붙여 넣으세요. 가져오기와 동일한 계정 이름을 사용하세요.')+'</p><button class="choice" id="copyKey">'+l('Copy pairing key','연결 키 복사')+'</button>');
 $('alerts').onchange=async e=>{await api('notifications',{enabled:e.target.checked});await refresh(true);};
 $('copyKey').onclick=async()=>{await navigator.clipboard.writeText(info.port+':'+info.token);toast(l('Pairing key copied','연결 키 복사됨'));};
 $('file').onchange=async e=>{
  const file=e.target.files[0];if(!file)return;$('importResult').textContent=l('Importing…','가져오는 중…');
  try{const parameters=new URLSearchParams({provider:$('provider').value,account:$('account').value});
   const response=await fetch('/import?'+parameters,{method:'POST',headers:{'Content-Type':'application/octet-stream'},body:file});const d=await response.json();if(!response.ok)throw Error(d.error||'Import failed');
   $('importResult').textContent=l('Imported ','가져온 세션 ')+d.count;await refresh(true);
  }catch(err){$('importResult').textContent=err.message;}
 };
}
document.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;const k=b.dataset;
 try{
  if(k.lang){lang=k.lang;save('lang',lang);applyLanguage();}
  if(k.view){view=k.view;filter=null;redraw();}
  if(k.project){filter=filter===k.project?null:k.project;view='map';redraw();}
  if(k.fold){folded.has(k.fold)?folded.delete(k.fold):folded.add(k.fold);save('folded',[...folded]);redraw();}
  if(k.card)await detail(k.card,k.seq);
  if(k.pick){if($('dialog').open)$('dialog').close();pin(k.pick,true);$('mode').value='send';preview();$('prompt').focus();}
  if(k.hide){$('dialog').close();await edit(k.hide,{hidden:true});}
  if(k.restore)await edit(k.restore,{hidden:false});
  if('clear' in k){focus=null;locked=false;save('target',null);save('locked',false);$('mode').value='auto';redraw();}
  if(k.select){const m=messages[Number(k.n)];if(m.chosen)return;m.chosen=k.select;renderChat();try{await handle(item(k.select),m.body,m.action,m.key,m.found?.find(f=>f.session===k.select));save('chat',messages);}catch(err){delete m.chosen;renderChat();throw err;}}
  if('copy' in k)await navigator.clipboard.writeText(messages[Number(k.copy)].body);
  if(b.id==='closeDialog')$('dialog').close();
  if(b.id==='connections')await settings();
  if(b.id==='addGroup'){const name=prompt(l('Project name','프로젝트 이름'));if(name?.trim()){await api('group',{name:name.trim()});await refresh(true);}}
  if(b.id==='viewAction'){const gs=state.groups.filter(g=>!filter||filter===g.id),collapse=!gs.every(g=>folded.has(g.id));gs.forEach(g=>collapse?folded.add(g.id):folded.delete(g.id));save('folded',[...folded]);redraw();}
 }catch(err){toast(err.message);}
});
$('mode').onchange=preview;$('prompt').oninput=()=>{draftID=null;save('draftID',null);save('draft',$('prompt').value);preview();
 clearTimeout(searchTimer);const q=$('prompt').value.trim();if(q)searchTimer=setTimeout(()=>search(q).then(preview,()=>{}),250);};
$('prompt').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();$('composer').requestSubmit();}};
document.addEventListener('dragstart',e=>{const c=e.target.closest('[data-card]');if(c){dragging=true;e.dataTransfer.setData('application/x-thread-atlas',c.dataset.card);e.dataTransfer.effectAllowed='move';}});
const zone=e=>e.target.closest('#hiddenDrop,#composer,[data-group]');
const clean=()=>document.querySelectorAll('.dropping').forEach(x=>x.classList.remove('dropping'));
document.addEventListener('dragover',e=>{const z=zone(e);if(z&&e.dataTransfer.types.includes('application/x-thread-atlas')){e.preventDefault();clean();z.classList.add('dropping');}});
document.addEventListener('dragend',()=>{dragging=false;clean();});
document.addEventListener('drop',async e=>{const z=zone(e),id=e.dataTransfer.getData('application/x-thread-atlas');dragging=false;clean();if(!z||!item(id))return;e.preventDefault();
 try{if(z.id==='hiddenDrop')await edit(id,{hidden:true});else if(z.id==='composer'){pin(id,true);$('mode').value='send';preview();$('prompt').focus();}else await edit(id,{group:z.dataset.group,before:e.target.closest('[data-card]')?.dataset.card});}catch(err){toast(err.message);}
});
async function openNotification() {
 const match=/^#reply-([1-9][0-9]{0,18})$/.exec(location.hash);
 if(!match && location.hash!=='#inbox')return;
 view='inbox';filter=null;redraw();
 if(!match)return;
 try {
  const reply=await api('reply?seq='+match[1]);
  const thread=reply&&item(reply.session);
  if(!thread||thread.hidden){toast(l('This reply is no longer visible.','이 답변은 현재 볼 수 없습니다.'));return;}
  dialog(name(thread),'<p class="sub">'+escape(thread.provider)+' · '+stamp(reply.at)+'</p><div class="history"><section><p>'+escape(reply.body)+'</p></section></div><div class="actions"><button class="choice" data-card="'+thread.id+'">'+l('View source','원문 보기')+'</button><button class="choice" data-pick="'+thread.id+'">'+l('Reply here','이어서 답장')+'</button></div>');
 } catch(err){toast(err.message);}
}
$('prompt').value=load('draft','');applyLanguage();await refresh(true);
window.addEventListener('hashchange',openNotification);await openNotification();setInterval(()=>refresh(),2000);
