// Scores distinctive terms across the visible workspace. A near tie requires a choice.
const stops=new Set(('the a an to on for at in my me this that from with it is was are be yesterday please work thread status latest then what where did we leave can you should run set change show find update policy project session 계속 오늘 어제 어디까지 상태 확인 현재 부탁 좀 해줘 알려줘 진행해 작업').split(' '));
function words(text){
  const raw=String(text||'').toLowerCase().normalize('NFKC').match(/[\p{L}\p{N}]+/gu)||[];
  return [...new Set(raw.map(w=>w.replace(/^(.{2,}?)(에서|에게|으로|에는|에서의|의|에|을|를|는|은|이|가|로|까지)$/u,'$1').replace(/^([a-z]{3,})ies$/,'$1y').replace(/^([a-z]{3,})s$/,'$1')).filter(w=>w.length>1&&!stops.has(w)))];
}
// The provider a request names, if any. Routing and full-history search are both limited to it.
export function named(text){
  return [['Claude Code',/claude\s*code|클로드\s*코드/i],['ChatGPT',/chatgpt|챗지피티/i],['Codex',/codex|코덱스/i],['Claude',/claude|클로드/i]].find(([,r])=>r.test(text))?.[0];
}
// `found` holds full-history search results: the best-matching message of each session and the query terms it
// contains, as returned by /api/search.
export function route(text,items,focus,mode='auto',locked=false,found=[]){
  const visible=items.filter(i=>!i.hidden);
  const waiting=/waiting (?:on|for) (?:me|my)|need(?:s)? (?:my|a) reply|awaiting my reply|내 (답변|답장).*기다|답변.*대기/i.test(text);
  const lookup=/^(?:find|show|where|what happened|what is|what.s|which|how far|any updates|status\b)|어디|찾아|최근|상태.*알려|어떻게 됐/i.test(text.trim());
  const action=mode==='send'||(mode==='auto'&&!waiting&&!lookup);
  if(mode!=='send'&&waiting)return {action:false,candidates:visible.filter(i=>i.state==='wait'),waiting:true};
  // An explicit reply target wins over any provider name the message happens to mention.
  const pinned=visible.find(i=>i.id===focus);
  if(locked&&pinned&&action)return {action,candidates:[pinned]};
  const provider=named(text);
  const pool=visible.filter(i=>!provider||i.provider===provider);
  const selected=pool.find(i=>i.id===focus);
  const query=words(text);
  const generic=new Set('then test regression first again next 하나 다시 테스트 회귀 먼저'.split(' '));
  if(selected&&/^(?:then\b|그럼|그러면)/i.test(text.trim())&&query.every(q=>generic.has(q)))return {action,candidates:[selected]};
  const matched=new Map(found.map(f=>[f.session,f.terms]));
  const docs=pool.map(i=>({i,title:new Set(words(i.title)),body:new Set(words(i.latest)),history:new Set(matched.get(i.id)),group:new Set(words(i.project))}));
  const weights=new Map();
  for(const q of query){
    const freq=docs.filter(x=>x.title.has(q)||x.body.has(q)||x.history.has(q)||x.group.has(q)).length;
    if(freq)weights.set(q,1+Math.log((docs.length+1)/(freq+1)));
  }
  const scores=docs.map(d=>{
    let score=0,hits=0,titled=false;
    for(const [q,weight] of weights){
      const place=d.title.has(q)?3:d.body.has(q)?1.5:d.history.has(q)?1:d.group.has(q)?0.65:0;
      score+=weight*place;
      // Words found only in an older message can find a thread, but sending to it unasked needs more than that.
      if(place&&place!==1){hits++;titled=titled||place===3;}
    }
    return {i:d.i,score,strong:titled||hits>1};
  }).sort((a,b)=>b.score-a.score);
  const best=scores[0]?.score||0;
  // One incidental word in a reply or project name finds a thread, but is not enough to send to it unasked.
  const only=(i,strong)=>action&&!strong?{action,candidates:[i],confirm:true}:{action,candidates:[i]};
  if(best>=2 && (scores.length===1||best-scores[1].score>=Math.max(1,best*.22)))return only(scores[0].i,scores[0].strong);
  if(best){
    const close=scores.filter(x=>x.score>=best*.65).slice(0,8).map(x=>x.i);
    return close.length===1?only(close[0],false):{action,candidates:close};
  }
  if(selected&&/\b(then|go ahead|continue|proceed|do it|run the tests|that thread|yes|ok)\b|그럼|그래|진행해|이어서|계속|테스트부터/i.test(text))return {action,candidates:[selected]};
  const rest=pool.slice(0,8);
  return rest.length===1?only(rest[0],false):{action,candidates:rest};
}
