(function(){
'use strict';
const $=id=>document.getElementById(id);const body=document.body,query=$('query'),form=$('ask-form'),send=$('send'),dialogue=$('dialogue'),drawer=$('drawer'),shade=$('drawer-shade'),drawerContent=$('drawer-content'),steps=[...document.querySelectorAll('.reveal')];
const adapter=window.JLBackendAdapter;
const reduce=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const glyph='<span class="ai-mark" aria-hidden="true">巨</span>';
let supportsJobs=false,activeJob=null,uploading=0,pendingSupplement=null,draftTimeout=null,notificationTimer=null;
let timers=[],sequence=0,busy=false,conversationId=null,currentUser=null,currentOrg=null,attachments=[],toastTimeout=null,priorFocus=null,ready=false,historyItems=[],historyOffset=0,countFrame=0;
const create=(tag,cls,text)=>{const e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e};
function later(t,fn){const id=setTimeout(fn,t);timers.push(id)}
function clearTimers(){timers.forEach(clearTimeout);timers=[]}
function finishIntro(){sequence++;clearTimers();body.classList.remove('is-intro','is-unveiling');body.classList.add('is-ready');steps.forEach(x=>x.classList.add('shown'))}
function playIntro(){sequence++;clearTimers();closeDrawer();body.classList.remove('is-ready','is-unveiling','skip-immediate');body.classList.add('is-intro');requestAnimationFrame(()=>requestAnimationFrame(()=>{if(body.classList.contains('is-intro'))finishIntro()}))}
function showToast(message){const e=$('toast');e.textContent=message;e.classList.add('open');clearTimeout(toastTimeout);toastTimeout=setTimeout(()=>e.classList.remove('open'),4500)}
function showConnectionError(message){$('connection-banner').textContent=message;$('connection-banner').classList.add('show');$('top-status').classList.add('failed');$('top-status').textContent='服务连接异常'}
function clearConnectionError(){$('connection-banner').classList.remove('show');$('top-status').classList.remove('failed');$('top-status').textContent='企业智能助理已就绪'}
// 标题写日期，不写“欢迎回来”；下面只留星期和人名，不写问候语。
function dateTitle(){const d=new Date();return (d.getMonth()+1)+'月'+d.getDate()+'日'}
function helloLine(){const sal=currentUser&&(currentUser.salutation||currentUser.display_name);const week='周'+'日一二三四五六'[new Date().getDay()];return sal?week+' · '+sal:week}
// —— 主动汇报：登录后先扫一眼今天的单子和账，把要拍板的事摆到桌面上 ——
function setTopStatus(text,state){const el=$('top-status');if(!el||el.classList.contains('failed'))return;el.textContent=text;el.classList.remove('scanning','attention','ready');if(state)el.classList.add(state)}
function todayTone(routine){return /回单/.test(String(routine||''))?'receipt':'check'}
function todayQuestion(item){const data=item||{},num=String(data.waybill_number||''),routine=String(data.routine||'');if(todayTone(routine)==='receipt')return '帮我看看运单 '+num+' 的回单，怎么催？';if(/对账|差异|费用/.test(routine))return '帮我看看运单 '+num+' 的对账差异，这笔费用该怎么处理？';return '帮我看看运单 '+num+' 的'+routine+'，怎么处理？'}
function todayCard(item){
 const data=item||{},routine=String(data.routine||'待办'),tone=todayTone(routine);
 const card=create('article','today-card is-'+tone);
 const id=create('div','tr-id'),num=create('span','tr-num');
 num.append(create('small','','运单'),document.createTextNode(String(data.waybill_number||'—')));
 const kind=create('span','tr-kind');kind.append(create('i'),document.createTextNode(routine));
 id.append(num,kind);card.append(id);
 const bodyEl=create('div','tr-body');
 bodyEl.append(create('p','tr-evidence',String(data.evidence||'这一票需要你看一下。')));
 const facts=[];
 if(data.cargo)facts.push(['货物',data.cargo]);
 if(tone==='receipt'){if(data.receiver)facts.push(['收货人',data.receiver]);if(data.destination)facts.push(['送往',data.destination])}
 else if(data.carrier)facts.push(['承运',data.carrier]);
 if(facts.length){const meta=create('div','tr-meta');for(const pair of facts){const cell=create('span');cell.append(create('b','',pair[0]),document.createTextNode(' '+String(pair[1])));meta.append(cell)}bodyEl.append(meta)}
 card.append(bodyEl);
 const btn=create('button','tr-go');btn.type='button';btn.append(document.createTextNode('去处理'),create('i','','→'));
 btn.addEventListener('click',()=>{if(!ready){showToast('还在连接业务服务，稍等一下。');return}submit(todayQuestion(data))});
 card.append(btn);
 return card;
}
// 数字滚动生长：0 → 目标值，一次镜面扫光同步走。reduced-motion 直接落值。
function animateCount(total){
  const el=$('today-count'),box=$('hero-count'),live=$('today-count-status');
  if(!el||!box)return;
  cancelAnimationFrame(countFrame);
  const value=Math.max(0,Math.round(Number(total)||0));
  box.classList.remove('is-counting');
  if(reduce||document.hidden||value===0){
    el.textContent=String(value);
    if(live)live.textContent=value>0?'今天 '+value+' 件等你处理':'';
    return;
  }
  void box.offsetWidth;
  box.classList.add('is-counting');
  const start=performance.now(),dur=1000;
  const step=now=>{
    const t=Math.min(1,(now-start)/dur),eased=1-Math.pow(1-t,3);
    el.textContent=String(Math.round(value*eased));
    if(t<1){countFrame=requestAnimationFrame(step)}
    else{el.textContent=String(value);box.classList.remove('is-counting');if(live)live.textContent='今天 '+value+' 件等你处理'}
  };
  countFrame=requestAnimationFrame(step);
}
function renderToday(result){
 const routine=(result&&result.routine)||{},items=Array.isArray(routine.routine_candidates)?routine.routine_candidates:[];
 const counted=Number(routine.total),total=Number.isFinite(counted)?Math.max(counted,items.length):items.length;
 $('welcome-title').textContent=dateTitle();
 $('hero-hello').textContent=helloLine();
 const checks=items.filter(item=>todayTone(item.routine)!=='receipt').length,receipts=items.length-checks;
 $('hero-count').hidden=total<=0;
 animateCount(total);
 const bits=[];
 if(items.length===total&&total>0){if(checks)bits.push(checks+' 笔费用要核');if(receipts)bits.push(receipts+' 票回单没回')}
 $('brief-line').textContent=total>0?(bits.length?bits.join('，')+'。':'按运单排好了，先看最急的。'):'今天没有要盯的事，有单子随时问我。';
 if(total>0)setTopStatus('等你处理 '+total+' 件','attention');else setTopStatus('今天没有要盯的事','ready');
 $('list-count').textContent=total>0?'共 '+total+' 件':'0 件';
 const wrap=$('today-cards');wrap.replaceChildren();
 if(items.length)items.forEach((item,i)=>{const card=todayCard(item);card.style.setProperty('--i',String(Math.min(i,9)));wrap.append(card)});
 else wrap.append(create('p','welcome-empty','今天先没有要盯的事。'));
 const note=$('brief-note'),noteText=typeof routine.note==='string'?routine.note.trim():'';
 note.textContent=noteText;note.hidden=!noteText;
}
async function loadToday(){
 setTopStatus('正在扫账','scanning');
 $('brief-line').textContent='我先扫一眼今天的单子和账。';
 try{renderToday(await adapter.today())}
 catch(_){
  $('welcome-title').textContent=dateTitle();
  $('hero-hello').textContent=helloLine();
  $('hero-count').hidden=true;
  $('brief-line').textContent='今天暂时没能扫全账，有单子随时问我。';
  setTopStatus('服务已连上','ready');
 }
}
function setIdentity(data){$('agent-usage').hidden=!data.capabilities?.team_operations;$('agent-routines').hidden=!data.capabilities?.durable_jobs;supportsJobs=!!data.capabilities?.durable_jobs;body.classList.toggle('preview-mode',!!data.preview);currentUser=data.user||{};currentOrg=data.organization||{};$('brand-name').textContent=currentOrg.name||'企业工作台';$('welcome-title').textContent=dateTitle();$('hero-hello').textContent=helloLine();$('identity').textContent=(currentUser.display_name||'用').slice(0,1);$('identity').title=(currentUser.display_name||'当前账号')+' · 点击退出登录';$('identity').setAttribute('aria-label',(currentUser.display_name||'当前账号')+'，点击退出登录');if(data.workbench_url){$('workbench-link').href=data.workbench_url}else{$('workbench-link').hidden=true}$('footer-note').textContent=data.preview?(data.model_mode==='real'?'真实模型 · 测试数据':'本地功能验收 · 合成数据 · 测试模型'):'当前企业：'+(currentOrg.name||'企业')+' · 数据来自业务服务';$('voice').hidden=!data.capabilities?.voice;if(data.capabilities&&data.capabilities.attachments){$('attach').hidden=false}if(Array.isArray(data.suggestions)&&data.suggestions.length){const parent=$('suggestions');parent.replaceChildren();for(const item of data.suggestions){const b=create('button','suggest',item.label);b.dataset.ask=item.prompt;parent.append(b)}}}
function storageKey(){return 'jl.ai.last-thread.'+String(currentOrg&&currentOrg.id||'unknown')+'.'+String(currentUser&&currentUser.id||'unknown')}
function lastThread(){try{return localStorage.getItem(storageKey())}catch(_){return null}}
function saveLastThread(id){try{localStorage.setItem(storageKey(),id)}catch(_){}}
function forgetLastThread(){try{localStorage.removeItem(storageKey())}catch(_){}}
function startConversation(){if(body.classList.contains('is-intro')||body.classList.contains('is-unveiling'))finishIntro();body.classList.add('chat-on')}
function scrollEnd(smooth=false){if(!body.classList.contains('chat-on'))return;requestAnimationFrame(()=>dialogue.scrollTo({top:dialogue.scrollHeight,behavior:smooth&&!reduce?'smooth':'instant'}))}
function userMessage(s,blocks=[]){const e=create('div','message user'),b=create('div','user-bubble',s);b.style.whiteSpace='pre-wrap';e.append(b);if(blocks.length){const refs=create('div','user-file-links');for(const block of blocks)appendCard(refs,block);e.append(refs)}dialogue.append(e);return e}
function assistantSkeleton(){const e=create('div','message assistant');const avatar=create('div','ai-avatar');avatar.innerHTML=glyph;const c=create('div','message-content');c.append(create('div','msg-caption','企业智能助理'));const thinking=create('div','thinking');thinking.append(create('span','thinking-dot'),create('span','', '正在处理您的问题…'));c.append(thinking);e.append(avatar,c);dialogue.append(e);return e}
function appendCard(container,b){const type=b.type||'note';
 if(type==='file'){
  if(!/^[A-Za-z0-9_-]{10,80}$/.test(b.file_id||''))return;
  const a=create('a','case-trigger',b.label||'查看文件');a.href='/api/agent/files/'+encodeURIComponent(b.file_id);a.title=b.label||'业务文件';if(/\.(xlsx?|csv|zip|docx)$/i.test(b.label||'')){a.download=b.label;a.addEventListener('click',async event=>{event.preventDefault();try{const response=await fetch(a.href,{credentials:'same-origin'});if(!response.ok)throw new Error();const blob=await response.blob();const url=URL.createObjectURL(blob),download=document.createElement('a');download.href=url;download.download=b.label;document.body.append(download);download.click();download.remove();setTimeout(()=>URL.revokeObjectURL(url),60000)}catch(_){showToast('文件暂时取不到，请检查登录状态后再试。')}})}else{a.target='_blank';a.rel='noopener'};container.append(a);return;
 }
 if(type==='actions'){const el=create('div','followups');(b.items||[]).forEach(a=>{if(!a||!a.prompt)return;const btn=create('button','followup',a.label||a.prompt);btn.type='button';btn.dataset.ask=a.prompt;el.append(btn)});container.append(el);return}
 if(type==='case'){const btn=create('button','case-trigger',b.label||'查看业务案卷');btn.type='button';btn.dataset.open=b.case_id;container.append(btn);return}
 if(type==='note'){container.append(create('p','msg-p muted',b.text||''));return}
 const card=create('div','inline-card msg-section'),top=create('div','card-top');top.append(create('div','card-label',b.title||''));if(b.tag)top.append(create('span','card-badge',b.tag));card.append(top);
 if(type==='metrics'){const grid=create('div','values');(b.items||[]).forEach((v,i)=>{const col=create('div','value');col.append(create('div','name',v.label||''),create('div','amount'+(v.emphasis||i===(b.items||[]).length-1?' gold':''),String(v.value??'')));grid.append(col)});card.append(grid)}
 else if(type==='list'){const list=create('div','line-list');(b.items||[]).forEach(v=>{const r=create('div','line-item');r.append(create('div','line-id',String(v.id||'')),create('div','line-meta',v.detail||''));if(v.case_id){const btn=create('button','line-open','看这票 ›');btn.type='button';btn.dataset.open=v.case_id;r.append(btn)}list.append(r)});card.append(list)}
 else if(type==='checklist'){const list=create('div','check-list');(b.items||[]).forEach((s,i)=>{const r=create('div','check-row');r.append(create('span','check-icon',String(i+1)),create('span','',String(s)));list.append(r)});card.append(list)}
 else if(type==='table'){const wrap=create('div','ai-table-wrap'),tbl=create('table','ai-table'),head=create('thead'),hr=create('tr');(b.columns||[]).forEach(c=>hr.append(create('th','',c)));head.append(hr);tbl.append(head);const tbody=create('tbody');(b.rows||[]).forEach(row=>{const tr=create('tr');(row||[]).forEach(cell=>tr.append(create('td','',String(cell??''))));tbody.append(tr)});tbl.append(tbody);wrap.append(tbl);card.append(wrap)}
 if(b.footer)card.append(create('div','card-foot',b.footer));if(b.case_id){const actions=create('div','card-actions'),btn=create('button','card-btn','查看原始案卷');btn.type='button';btn.dataset.open=b.case_id;actions.append(btn);card.append(actions)}container.append(card)
}
// Render model-authored formatting with DOM text nodes only: never interpret model output as HTML.
function appendFormattedInline(parent,text){
 const pattern=/(\*\*[^*\n]+\*\*|`[^`\n]+`|\[v3:waybill:[^:\]\n]+:r\d+\]|\[v3:(?:operational-summary|recent-changes):[^\]\n]+\]|\[rule:[^\]\n]+\])/g;let cursor=0,match;
 while((match=pattern.exec(text))){
  if(match.index>cursor)parent.append(document.createTextNode(text.slice(cursor,match.index)));
  const value=match[0];
  if(value.startsWith('**'))parent.append(create('strong','',value.slice(2,-2)));
  else if(value.startsWith('`'))parent.append(create('code','',value.slice(1,-1)));
  else if(value.startsWith('[v3:waybill:')){const parts=value.slice(1,-1).split(':');const button=create('button','answer-evidence','查看运单依据');button.type='button';button.dataset.open=parts[2];parent.append(button)}
  else {const label=value.startsWith('[rule:')?'（公司规则）':value.startsWith('[v3:recent-changes:')?'（业务变动记录）':'（业务记录）';const note=create('span','answer-source',label);note.title=value;parent.append(note)}
  cursor=pattern.lastIndex;
 }
 if(cursor<text.length)parent.append(document.createTextNode(text.slice(cursor)));
}
function renderFormatted(container,text){
 const lines=String(text||'').split(/\r?\n/);let i=0;
 const cells=line=>line.trim().replace(/^\|/,'').replace(/\|$/,'').split('|').map(x=>x.trim());
 const isSeparator=line=>/^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line||'');
 while(i<lines.length){
  const line=lines[i];if(!line.trim()){i++;continue}
  if(line.includes('|')&&isSeparator(lines[i+1])){
   const headers=cells(line),wrap=create('div','answer-table-wrap'),table=create('table','answer-table'),head=create('thead'),tr=create('tr');
   headers.forEach(value=>{const th=create('th');appendFormattedInline(th,value);tr.append(th)});head.append(tr);table.append(head);i+=2;
   const tbody=create('tbody');while(i<lines.length&&lines[i].trim()&&lines[i].includes('|')){const row=create('tr'),values=cells(lines[i]);headers.forEach((_,j)=>{const td=create('td');td.dataset.label=headers[j];appendFormattedInline(td,values[j]||'');row.append(td)});tbody.append(row);i++}table.append(tbody);wrap.append(table);container.append(wrap);continue;
  }
  const listMatch=line.match(/^\s*(?:([-*])\s+|(\d+)[.、]\s*)/);
  if(listMatch){const ordered=!!listMatch[2],list=create(ordered?'ol':'ul','answer-list');while(i<lines.length){const item=lines[i].match(/^\s*(?:([-*])\s+|(\d+)[.、]\s*)/);if(!item||!!item[2]!==ordered)break;const li=create('li');appendFormattedInline(li,lines[i].slice(item[0].length));list.append(li);i++}container.append(list);continue}
  const heading=line.match(/^#{1,3}\s+(.+)/);const element=create(heading?'h3':'p',heading?'answer-heading':'msg-p');appendFormattedInline(element,heading?heading[1]:line);container.append(element);i++;
 }
}
function renderAssistant(message, el){let target=el;if(!target){target=assistantSkeleton()}const content=target.querySelector('.message-content');content.replaceChildren(create('div','msg-caption','企业智能助理'));renderFormatted(content,message.text);for(const b of message.blocks||[]){appendCard(content,b)}scrollEnd(true);return target}
function renderHistory(items){dialogue.replaceChildren();for(const m of items||[]){if(m.role==='user')userMessage(m.text||m.content||'',m.blocks||[]);else if(m.role==='assistant')renderAssistant(m)}if((items||[]).length)startConversation();scrollEnd()}
function showLogin(message=''){const shade=$('auth-shade');shade.classList.add('open');shade.setAttribute('aria-hidden','false');$('login-error').hidden=!message;$('login-error').textContent=message;if(!message)setTimeout(()=>$('login-username').focus(),0)}
function hideLogin(){const shade=$('auth-shade');shade.classList.remove('open');shade.setAttribute('aria-hidden','true')}
async function submitLogin(event){event.preventDefault();const form=event.currentTarget,button=$('login-submit');button.disabled=true;$('login-error').hidden=true;try{await adapter.login(form.elements.username.value.trim(),form.elements.password.value);form.elements.password.value='';hideLogin();await init()}catch(err){showLogin(err.message||'登录失败，请检查账号和密码。')}finally{button.disabled=false}}
async function signOut(){saveDraft();try{await adapter.logout()}catch(_){}ready=false;conversationId=null;currentUser=null;dialogue.replaceChildren();body.classList.remove('chat-on');query.disabled=true;send.disabled=true;closeDrawer();closeHistory();showLogin('已退出登录。请使用企业账号重新登录。')}
async function init(){try{const data=await adapter.bootstrap();setIdentity(data);ready=true;clearConnectionError();hideLogin();loadToday();const id=lastThread();if(id&&id!=='null'){try{const chat=await adapter.getConversation(id);if(chat.redacted){conversationId=null;forgetLastThread();renderHistory(chat.messages||[]);showToast('这是旧AI历史的安全占位，不能在V3继续；请新建对话。')}else{conversationId=id;renderHistory(chat.messages||[])}}catch(_){forgetLastThread();conversationId=null}}send.disabled=false;query.disabled=false;loadDraft();resumeJob();checkNotifications();if(!notificationTimer)notificationTimer=setInterval(checkNotifications,10000);}catch(err){ready=false;send.disabled=true;query.disabled=true;if(err.status===401)showLogin('登录会话无效，请重新登录。');showConnectionError(err.status===401?'请登录后使用企业智能助理。':'无法连接到 Agent 业务服务。该页面不会使用演示回答代替真实数据。');$('footer-note').textContent='尚未连接业务服务';if(err.status!==401)showToast('业务服务暂不可用，请稍后重试。')}
}
async function ensureConversation(){if(!conversationId){const result=await adapter.createConversation();conversationId=result.conversation_id||null;if(conversationId)saveLastThread(conversationId)}return conversationId}
function resize(){query.style.height='auto';query.style.height=Math.min(query.scrollHeight,body.classList.contains('chat-on')?125:180)+'px'}
function renderAttachments(){const parent=$('attachment-list');parent.replaceChildren();attachments.forEach((a,i)=>{const chip=create('div','attachment-pill');chip.append(create('span','',a.name||'已上传文件'));const btn=create('button','', '×');btn.type='button';btn.title='移除附件';btn.addEventListener('click',()=>{attachments.splice(i,1);renderAttachments();saveDraft()});chip.append(btn);parent.append(chip)})}
function pendingKey(){return storageKey()+'.pending-job'}
function saveDraft(){if(!currentUser)return;try{localStorage.setItem(storageKey()+'.draft',JSON.stringify({text:query.value,attachments,pendingSupplement}))}catch(_){}}
function loadDraft(){try{const d=JSON.parse(localStorage.getItem(storageKey()+'.draft')||'{}');query.value=d.text||'';attachments=Array.isArray(d.attachments)?d.attachments:[];pendingSupplement=d.pendingSupplement&&typeof d.pendingSupplement.text==='string'?d.pendingSupplement:null;if(pendingSupplement&&!localStorage.getItem(pendingKey()))restoreSupplement();renderAttachments();resize()}catch(_){}}
function restoreSupplement(){if(!pendingSupplement)return;query.value=[pendingSupplement.text,query.value].filter(Boolean).join('\n');attachments=[...(pendingSupplement.attachments||[]),...attachments].filter((v,i,a)=>v&&v.id&&a.findIndex(x=>x&&x.id===v.id)===i);pendingSupplement=null;renderAttachments();resize();saveDraft()}
function submitSupplement(){if(!pendingSupplement||busy||!ready)return;if(uploading){setTimeout(submitSupplement,300);return}const next=pendingSupplement,unsentText=query.value,unsentFiles=attachments;pendingSupplement=null;query.value=next.text;attachments=next.attachments||[];renderAttachments();saveDraft();submit(next.text);query.value=unsentText;attachments=unsentFiles;renderAttachments();resize();saveDraft()}
function busyUI(on){busy=on;$('add-context').hidden=!on;send.disabled=!ready||(on&&!supportsJobs);send.setAttribute('aria-label',on?(supportsJobs?'停止处理':'正在处理'):'发送问题');send.title=on?'停止后续处理':'发送问题';send.innerHTML=on?'<span aria-hidden="true">■</span>':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>'}
function jobProgress(el,data){const t=el.querySelector('.thinking');if(t)t.lastChild.textContent=data.progress||'正在处理'}
async function stopJob(){if(activeJob){await adapter.cancelJob(activeJob);showToast('正在停止后续处理，已完成的操作会保留。')}}
async function submit(q){
 const text=String(q||'').trim();if(uploading){showToast('资料还在接收，稍等一下。');return}if((!text&&!attachments.length)||!ready)return;
 if(busy){const nextFiles=attachments.splice(0);pendingSupplement={text:[pendingSupplement?.text,text].filter(Boolean).join('\n'),attachments:[...(pendingSupplement?.attachments||[]),...nextFiles].filter((v,i,a)=>v&&v.id&&a.findIndex(x=>x&&x.id===v.id)===i)};query.value='';renderAttachments();resize();saveDraft();showToast('已记下补充内容，等这次处理完就接着看。');return}
 let completed=false;const attached=attachments.splice(0);busyUI(true);startConversation();query.value='';resize();userMessage(text||'请查看附件',attached.map(a=>({type:'file',file_id:a.id,label:a.name})));scrollEnd();const el=assistantSkeleton();scrollEnd();
 renderAttachments();saveDraft();
 try{
  const id=await ensureConversation();const jobKey=crypto.randomUUID();
  if(supportsJobs)try{localStorage.setItem(pendingKey(),JSON.stringify({id:null,key:jobKey,conversation_id:id,text,attached}))}catch(_){}
  const res=await adapter.send({conversation_id:id,text,attachment_ids:attached.map(a=>a.id),jobKey,onProgress:d=>jobProgress(el,d),onJob:d=>{activeJob=d.job_id;conversationId=d.conversation_id;saveLastThread(conversationId);try{localStorage.setItem(pendingKey(),JSON.stringify({id:activeJob,key:jobKey,conversation_id:conversationId,text,attached}))}catch(_){}}});
  conversationId=res.conversation_id||id;if(conversationId)saveLastThread(conversationId);renderAssistant(res.message||res,el);clearConnectionError();completed=true;
  if(window.parent!==window)window.parent.postMessage({type:'jl-agent-result-ready'},location.origin);
 }catch(err){
  el.querySelector('.message-content').replaceChildren(create('div','msg-caption','企业智能助理'),create('p','msg-p',err.message||'这次没有完成，原资料仍保留。'));
  if(err.status===401)showLogin('登录会话已失效，请重新登录。');attachments=attached.concat(attachments);renderAttachments();
  if(!query.value&&!pendingSupplement)query.value=text;resize();saveDraft();
 }finally{
  try{localStorage.removeItem(pendingKey())}catch(_){}activeJob=null;busyUI(false);scrollEnd(true);
  if(pendingSupplement){if(completed)submitSupplement();else{restoreSupplement();showToast('上次处理没有完成，补充内容已留在输入框。')}}
 }
}
async function resumeJob(){
 if(!supportsJobs)return;
 let saved;try{saved=JSON.parse(localStorage.getItem(pendingKey())||'null')}catch(_){}
 if(!saved)return;
 let completed=false;activeJob=saved.id;busyUI(true);startConversation();const el=assistantSkeleton();
 try{const res=saved.id?await adapter.watchJob(saved.id,d=>jobProgress(el,d)):await adapter.send({conversation_id:saved.conversation_id,text:saved.text,attachment_ids:(saved.attached||[]).map(a=>a.id),jobKey:saved.key,onProgress:d=>jobProgress(el,d),onJob:d=>{activeJob=d.job_id;saved.id=d.job_id;localStorage.setItem(pendingKey(),JSON.stringify(saved))}});conversationId=res.conversation_id||saved.conversation_id||conversationId;if(conversationId)saveLastThread(conversationId);try{const thread=await adapter.getConversation(conversationId);renderHistory(thread.messages||[])}catch(_){renderAssistant(res.message||res,el)}completed=true}
 catch(err){el.querySelector('.message-content').replaceChildren(create('p','msg-p',err.message));if(!query.value)query.value=saved.text||'';attachments=(saved.attached||[]).concat(attachments);renderAttachments();saveDraft()}
 finally{try{localStorage.removeItem(pendingKey())}catch(_){}activeJob=null;busyUI(false);if(pendingSupplement){if(completed)submitSupplement();else restoreSupplement()}}
}
async function receiveFiles(list){
 for(const file of list){
  if(attachments.length>=12){showToast('一次最多12份资料');break}
  if(file.size>10*1024*1024){showToast(file.name+' 超过10MB');continue}
  uploading++;const temp={id:null,name:file.name+' · 接收中'};attachments.push(temp);renderAttachments();
  try{const item=await adapter.upload(file);attachments=attachments.filter(x=>x!==temp);if(!attachments.some(x=>x.id===item.id))attachments.push({id:item.id,name:item.name});}
  catch(err){attachments=attachments.filter(x=>x!==temp);showToast('没有接收到 '+file.name+'，可以再发一次。')}
  finally{uploading--;renderAttachments();saveDraft()}
 }
}
async function checkNotifications(){
 if(!ready||!supportsJobs||document.hidden)return;
 try{
  const data=await adapter.notifications(),items=data.items||[],button=$('agent-updates');
  button.hidden=!items.length;button.textContent=items.length?'有 '+items.length+' 条新进展':'';
  button.onclick=()=>{
   const root=$('agent-notifications');
   root.replaceChildren(create('div','notif-head','新进展'));
   for(const item of items){
    const entry=create('button','history-item',item.text);entry.type='button';
    entry.onclick=async()=>{
     if(busy||pendingSupplement){showToast('等当前事情处理完，就能查看这条进展。');return}
     try{
      if(item.scan_id)await openRoutineScan(item.scan_id);
      else if(item.conversation_id)await restoreThread(item.conversation_id);
      else{query.value='帮我看看这条进展：'+item.text;resize();saveDraft();query.focus()}
      await adapter.seen(item.id);root.hidden=true;checkNotifications();
     }catch(_){showToast('这条进展暂时无法读取。')}
    };
    root.append(entry);
   }
   root.hidden=!root.hidden;
  };
 }catch(_){}
}

async function openRoutineScan(id){
 const scan=await adapter.routineScan(id);
 drawer.classList.remove('usage-view');
 priorFocus=$('agent-updates');
 shade.classList.add('open');drawer.classList.add('open');drawer.setAttribute('aria-hidden','false');
 drawer.setAttribute('aria-label','扫账结果');$('drawer-label').textContent='扫账结果';
 const note=create('p','drawer-note',scan.note||'历史扫账候选，业务状态可能已变化。');
 drawerContent.replaceChildren(note);
 if(!scan.routine_candidates?.length)drawerContent.append(create('p','drawer-sub','这次扫描没有当前可查看的候选。'));
 for(const item of scan.routine_candidates||[]){
  const row=create('div','drawer-row');
  row.append(create('strong','',item.waybill_number),create('span','',item.evidence||item.kind));
  drawerContent.append(row);
 }
 $('close-drawer').focus();
}

function caseLabel(label){const raw=String(label||'');const indexed=raw.match(/^\s*(\d+)\s*[：:]\s*(type|status)\s*$/i);if(indexed)return `费用${indexed[2].toLowerCase()==='type'?'类型':'状态'} ${indexed[1]}`;const known={photo:'照片回单',paper:'纸质回单',delivered:'厂家交付',supplement:'补充状态'};return known[raw.toLowerCase()]||raw}
function caseValue(value){if(value==null)return '—';let status='',at='';if(typeof value==='object'&&!Array.isArray(value)){status=value.status||'';at=value.at||value.time||''}else if(typeof value==='string'){const legacy=value.match(/^\s*\{\s*['"]status['"]\s*:\s*['"]([^'"]*)['"](?:\s*,\s*['"](?:at|time)['"]\s*:\s*['"]([^'"]*)['"])?\s*\}\s*$/);if(legacy){status=legacy[1];at=legacy[2]||''}else return value.replace(/\b(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2})(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?/g,'$1 $2')}else return String(value);const stamp=String(at).replace('T',' ').replace(/:\d{2}(?:[+-]\d{2}:?\d{2}|Z)?$/,'');return [status,stamp].filter(Boolean).join(' · ')||'状态已记录'}
function renderCase(data){drawerContent.replaceChildren();drawerContent.append(create('h2','',data.title||'业务案卷'),create('p','drawer-sub',data.subtitle||''));for(const g of data.groups||[]){const block=create('div','drawer-group');block.append(create('h3','',g.title||''));for(const v of g.rows||[]){const row=create('div','drawer-row');row.append(create('span','',caseLabel(v.label)),create('strong','',caseValue(v.value)));block.append(row)}drawerContent.append(block)}if(data.files?.length){const section=create('div','drawer-group');section.append(create('h3','','共享原件'));for(const f of data.files)appendCard(section,{type:'file',file_id:f.id,label:f.filename});drawerContent.append(section)}if(data.note)drawerContent.append(create('div','drawer-note',data.note))}
async function openDrawer(id){if(!id)return;drawer.classList.remove('usage-view');$('drawer-label').textContent='业务案卷';drawer.setAttribute('aria-label','业务案卷');priorFocus=document.activeElement;shade.classList.add('open');drawer.classList.add('open');drawer.setAttribute('aria-hidden','false');drawerContent.replaceChildren(create('p','drawer-sub','正在读取案卷…'));$('close-drawer').focus();try{const data=await adapter.getCase(id);renderCase(data)}catch(err){drawerContent.replaceChildren(create('p','drawer-note','案卷读取失败：'+err.message))}}
async function openUsage(scope=(currentUser?.role==='owner_finance'?'company':'mine')){
 drawer.classList.add('usage-view');
 priorFocus=document.activeElement;shade.classList.add('open');drawer.classList.add('open');drawer.setAttribute('aria-hidden','false');drawer.setAttribute('aria-label','使用情况');$('drawer-label').textContent='使用情况';$('close-drawer').focus();drawerContent.replaceChildren(create('p','drawer-sub','正在读取使用情况…'));drawerContent.setAttribute('aria-live','polite');
 const person=currentUser?.id;
 try{
  const [use,alerts,health]=await Promise.all([adapter.usage(scope),adapter.eventAlerts(),currentUser?.role==='employee'?Promise.resolve(null):adapter.health()]);
  if(currentUser?.id!==person||!drawer.classList.contains('open'))return;
  const groups=[];
  if(scope==='company'&&currentUser?.role==='owner_finance')groups.push({title:'账户余额',rows:[{label:'可用余额',value:use.balance_status==='available'&&typeof use.balance==='number'?use.balance.toLocaleString()+' tokens':'暂未接入'}]});
  const count=(value,unknown)=>typeof value==='number'?value.toLocaleString()+' tokens'+(unknown?'（部分未报告）':''):'暂不可用';
  groups.push({title:'最近30天 · '+(scope==='company'?'全公司':'我的使用'),rows:[{label:'使用记录',value:use.calls+' 次'},{label:'已记录总用量',value:use.known_total_tokens.toLocaleString()+' tokens'},{label:'输入用量',value:count(use.known_input_tokens,use.unknown_input_calls)},{label:'输出用量',value:count(use.known_output_tokens,use.unknown_output_calls)},{label:'总用量未报告',value:use.unknown_usage_calls+' 次'}]});
  if(health){const labels={queued:'排队中',running:'处理中',succeeded:'已完成',failed:'未完成',interrupted:'重启时中断',cancelled:'已停止'};groups.push({title:'工作台运行',rows:[{label:'任务处理',value:health.worker_alive?'运行中':'尚未运行'},...health.jobs.map(x=>({label:labels[x.state]||x.state,value:x.count+' 件'}))]})}
  renderCase({title:scope==='company'?'公司 AI 用量':'我的 AI 用量',subtitle:'查看最近 30 天的使用情况。',groups,note:scope==='company'?'余额功能尚未接入；已记录用量来自模型返回的数据，不能作为收费账单。':'已记录用量来自模型返回的数据，不能作为收费账单。'});
  const actions=create('div','team-actions');if(currentUser?.role==='owner_finance'){const switcher=create('button','tiny',scope==='company'?'看我的使用':'看全公司');switcher.onclick=()=>openUsage(scope==='company'?'mine':'company');actions.append(switcher)}const refresh=create('button','tiny','刷新');refresh.onclick=()=>openUsage(scope);actions.append(refresh);drawerContent.append(actions);
  const label=create('label','team-toggle'),toggle=create('input');toggle.type='checkbox';toggle.checked=alerts.enabled;toggle.setAttribute('aria-label','提醒我的交接、逾期待办和资料到期');label.append(toggle,create('span','','提醒我的交接、逾期待办和资料到期'));drawerContent.append(label);
  toggle.onchange=async()=>{const enabled=toggle.checked;toggle.disabled=true;try{await adapter.setEventAlerts(enabled);showToast(enabled?'已开启，有新变化会告诉您。':'已关闭这类提醒。')}catch(e){toggle.checked=!enabled;showToast('设置未保存：'+e.message)}finally{toggle.disabled=false}};
  const detail=create('details','team-details');detail.append(create('summary','','查看逐次用量'));if(!use.items.length)detail.append(create('p','drawer-sub','这段时间还没有使用记录。'));for(const item of use.items){const raw=item.model_usage||{};const total=Number.isInteger(raw.total_tokens)?raw.total_tokens.toLocaleString()+' tokens':'用量未报告';const entry=create('p','drawer-sub',(scope==='company'?(item.owner_name||'已停用账号')+' · ':'')+new Date(item.created_at).toLocaleString('zh-CN')+' · '+total+' · '+(item.status===200?'已完成':'未完成'));if(scope==='mine'&&item.conversation_id){const b=create('button','tiny','回到对话');b.onclick=()=>{closeDrawer();restoreThread(item.conversation_id)};entry.append(document.createTextNode(' '),b)}detail.append(entry)}if(use.items_truncated)detail.append(create('p','drawer-note','这里展示最近100次，汇总包含完整期间。'));drawerContent.append(detail);
 }catch(e){drawerContent.replaceChildren(create('p','drawer-note','使用情况暂时没读出来：'+e.message));const retry=create('button','tiny','重新读取');retry.onclick=()=>openUsage(scope);drawerContent.append(retry)}
}
function scheduleText(schedule,everyMinutes){
 const raw=String(schedule||'').trim();
 if(raw){
  let m=raw.match(/^daily:(\d{1,2}):(\d{2})$/);if(m)return '每天 '+m[1].padStart(2,'0')+':'+m[2];
  m=raw.match(/^weekdays:(\d{1,2}):(\d{2})$/);if(m)return '工作日 '+m[1].padStart(2,'0')+':'+m[2];
  m=raw.match(/^interval:(\d+)$/);if(m)return '每 '+m[1]+' 分钟';
  return raw;
 }
 const minutes=Number(everyMinutes);
 return minutes>0?'每 '+minutes+' 分钟':'一次性';
}
function conditionText(condition){
 if(condition==='run_routine')return '定时扫账';
 if(condition==='remind')return '提醒';
 return '跟进';
}
function routineState(state){
 const known={pending:['运行中','rt-pending'],paused:['已暂停','rt-paused'],cancelled:['已取消','rt-cancelled'],done:['已完成','rt-done'],permission_changed:['权限变了，已停','rt-cancelled']};
 return known[state]||['已记录','rt-paused'];
}
function routineDue(value){
 if(!value)return '';
 const d=new Date(value);if(isNaN(d.getTime()))return '';
 const p=n=>String(n).padStart(2,'0');
 return (d.getMonth()+1)+'月'+d.getDate()+'日 '+p(d.getHours())+':'+p(d.getMinutes());
}
function routineRow(item,reload){
 const row=create('div','rt-item'),main=create('div','rt-main'),side=create('div','rt-side');
 main.append(create('div','rt-title',item.title||'未命名的定时任务'));
 const bits=[scheduleText(item.schedule,item.every_minutes),conditionText(item.condition)];
 if(item.waybill_number)bits.push('运单 '+item.waybill_number);
 main.append(create('div','rt-meta',bits.filter(Boolean).join(' · ')));
 const due=routineDue(item.due_at);
 if(due)main.append(create('div','rt-due',(item.state==='pending'?'下次：':'原定：')+due));
 const [label,cls]=routineState(item.state);
 side.append(create('span','rt-badge '+cls,label));
 if(item.state==='pending'||item.state==='paused'){
  const ops=create('div','rt-ops');
  const toggle=create('button','tiny',item.state==='pending'?'暂停':'恢复');
  toggle.type='button';toggle.onclick=()=>routineAction(item,item.state==='pending'?'pause':'resume',toggle,reload);
  const cancel=create('button','tiny ghost','取消');
  cancel.type='button';cancel.onclick=()=>routineAction(item,'cancel',cancel,reload);
  const edit=create('button','tiny','编辑');edit.type='button';edit.onclick=()=>routineForm(item,reload);
  ops.append(edit,toggle,cancel);side.append(ops);
 }
 row.append(main,side);return row;
}
async function routineAction(item,action,button,reload){
 const tip=action==='pause'?'已暂停，不会再推送。':action==='resume'?'已恢复，会按原时间继续。':'已取消，这条不会再推送。';
 const ops=button&&button.closest?button.closest('.rt-ops'):null;
 if(ops)ops.querySelectorAll('button').forEach(b=>{b.disabled=true});
 try{
  const result=await adapter.manageFollowup(action,item.id);
  if(result&&result.ok===false)throw new Error('服务端没有确认这次操作');
  showToast(tip);
  await reload();
 }catch(e){
  showToast('这次没操作成功：'+(e.message||'请稍后再试'));
  if(ops)ops.querySelectorAll('button').forEach(b=>{b.disabled=false});
 }
}
function routineForm(item,reload){
 const form=create('form','rt-form');
 const title=document.createElement('input');title.required=true;title.maxLength=200;title.placeholder='任务名称';title.value=item?.title||'';
 const due=document.createElement('input');due.type='datetime-local';due.required=true;
 if(item?.due_at){const d=new Date(item.due_at);d.setMinutes(d.getMinutes()-d.getTimezoneOffset());due.value=d.toISOString().slice(0,16)}
 const sched=document.createElement('input');sched.required=!item||item.condition==='run_routine';sched.placeholder='daily:08:00 / weekdays:08:00 / interval:30';sched.value=item?.schedule||'daily:08:00';
 const save=create('button','tiny','保存');save.type='submit';const cancel=create('button','tiny','返回');cancel.type='button';cancel.onclick=reload;
 form.append(title,due,sched,save,cancel);
 form.onsubmit=async e=>{e.preventDefault();save.disabled=true;try{
  const fields={title:title.value,due_at:new Date(due.value).toISOString(),schedule:sched.value};
  if(!item)Object.assign(fields,{condition:'run_routine',routine_kind:'daily_scan',request_key:crypto.randomUUID()});
  await adapter.manageFollowup(item?'edit':'create',item?.id||'',fields);showToast('定时任务已保存');await reload();
 }catch(err){showToast('保存失败：'+(err.message||'请检查输入'));save.disabled=false}};
 drawerContent.replaceChildren(form);title.focus();
}
async function renderRoutines(){
 let data;
 try{data=await adapter.followups()}catch(e){
  if(!drawer.classList.contains('open'))return;
  drawerContent.replaceChildren(create('p','rt-note','定时任务暂时没读出来：'+(e.message||'请求失败')));
  const retry=create('button','tiny','重新读取');retry.type='button';retry.onclick=()=>renderRoutines();
  drawerContent.append(retry);return;
 }
 if(!drawer.classList.contains('open'))return;
 const items=Array.isArray(data.items)?data.items:[];
 drawerContent.replaceChildren();
 const tools=create('div','rt-tools'),refresh=create('button','tiny','刷新');
 refresh.type='button';refresh.onclick=async()=>{refresh.disabled=true;await renderRoutines()};
 const add=create('button','tiny','新增扫账任务');add.type='button';add.onclick=()=>routineForm(null,renderRoutines);
 tools.append(refresh,add);drawerContent.append(tools);
 if(!items.length){drawerContent.append(create('p','rt-empty','还没有定时任务，可以让我帮你设一个，比如『每天早上 8 点扫账推给我』'));return}
 const list=create('div','rt-list');
 for(const item of items)list.append(routineRow(item,renderRoutines));
 drawerContent.append(list,create('p','rt-foot','暂停后不再推送，恢复后按原时间继续；取消后不能恢复。'));
}
async function openRoutines(){
 drawer.classList.remove('usage-view');
 priorFocus=document.activeElement;
 shade.classList.add('open');drawer.classList.add('open');drawer.setAttribute('aria-hidden','false');
 drawer.setAttribute('aria-label','定时任务');$('drawer-label').textContent='定时任务';$('close-drawer').focus();
 drawerContent.replaceChildren(create('p','rt-note','正在读取定时任务…'));
 await renderRoutines();
}
$('agent-routines').addEventListener('click',()=>openRoutines());
$('agent-usage').addEventListener('click',()=>openUsage());
function closeDrawer(){shade.classList.remove('open');drawer.classList.remove('open');drawer.setAttribute('aria-hidden','true');if(priorFocus&&priorFocus.isConnected&&priorFocus.focus)priorFocus.focus();priorFocus=null}
function closeHistory(){const panel=$('history-shade');panel.classList.remove('open');panel.setAttribute('aria-hidden','true')}
function renderHistoryList(){const root=$('history-items');root.replaceChildren();if(!historyItems.length){root.append(create('p','drawer-note','当前账号还没有可恢复的服务端对话。'));return}for(const item of historyItems){const button=create('button','history-item'),title=create('strong','',item.preview||'新对话'),meta=create('span','',`${item.updated_at||item.created_at||''} · ${item.message_count||0} 条消息`);button.type='button';button.append(title,meta);button.addEventListener('click',()=>restoreThread(item.conversation_id));root.append(button)}}
async function loadHistory(reset=true){if(reset){historyItems=[];historyOffset=0}$('history-items').replaceChildren(create('p','drawer-note','正在读取当前账号的历史…'));$('history-more').style.display='none';try{const result=await adapter.listConversations({limit:30,offset:historyOffset});const items=result.items||[];historyItems=reset?items:historyItems.concat(items);historyOffset=Number(result.offset||historyOffset)+items.length;renderHistoryList();$('history-more').style.display=items.length===30?'inline-block':'none'}catch(err){$('history-items').replaceChildren(create('p','drawer-note','无法读取服务端历史：'+(err.message||'请求失败')))}}
async function openHistory(){const panel=$('history-shade');panel.classList.add('open');panel.setAttribute('aria-hidden','false');await loadHistory(true)}
async function restoreThread(id){if(busy||pendingSupplement){showToast('等当前事情处理完，就能切换对话。');return}try{const thread=await adapter.getConversation(id);dialogue.replaceChildren();if(thread.redacted){conversationId=null;forgetLastThread();renderHistory(thread.messages||[]);showToast('这是旧AI历史的安全占位，不能在V3继续；请新建对话。')}else{conversationId=thread.conversation_id||id;saveLastThread(conversationId);renderHistory(thread.messages||[])}closeHistory();query.focus()}catch(err){showToast('无法恢复这段对话：'+(err.message||'请求失败'))}}
async function newChat(){if(busy||pendingSupplement){showToast('等当前事情处理完，就能开始新对话。');return}busy=true;query.disabled=true;send.disabled=true;$('new-chat').disabled=true;try{const result=await adapter.createConversation();conversationId=result.conversation_id||null;if(conversationId)saveLastThread(conversationId);else forgetLastThread();dialogue.replaceChildren();body.classList.remove('chat-on');closeDrawer();query.value='';attachments=[];renderAttachments();saveDraft();resize();query.focus()}catch(err){showToast('新对话创建失败：'+(err.message||'请求失败'))}finally{busy=false;query.disabled=!ready;send.disabled=!ready;$('new-chat').disabled=false;if(ready)query.focus()}}
form.addEventListener('submit',e=>{e.preventDefault();if(busy&&!query.value.trim())stopJob();else submit(query.value)});query.addEventListener('input',()=>{resize();clearTimeout(draftTimeout);draftTimeout=setTimeout(saveDraft,250)});query.addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();submit(query.value)}});
document.addEventListener('click',e=>{const a=e.target.closest('[data-ask]');if(a){submit(a.dataset.ask);return}const b=e.target.closest('[data-open]');if(b){openDrawer(b.dataset.open)}});
$('new-chat').addEventListener('click',newChat);shade.addEventListener('click',closeDrawer);$('close-drawer').addEventListener('click',closeDrawer);document.addEventListener('keydown',e=>{if(e.key==='Escape'){if(drawer.classList.contains('open'))closeDrawer();if($('history-shade').classList.contains('open'))closeHistory()}});
$('history').addEventListener('click',openHistory);$('close-history').addEventListener('click',closeHistory);$('history-shade').addEventListener('click',event=>{if(event.target===$('history-shade'))closeHistory()});$('history-more').addEventListener('click',()=>loadHistory(false));$('identity').addEventListener('click',signOut);$('login-form').addEventListener('submit',submitLogin);
$('attach').addEventListener('click',()=>$('attachment-input').click());
$('attachment-input').addEventListener('change',e=>{const selected=[...(e.target.files||[])];e.target.value='';receiveFiles(selected)});
form.addEventListener('paste',e=>{const images=[...(e.clipboardData?.files||[])];if(images.length){e.preventDefault();receiveFiles(images)}});
form.addEventListener('dragover',e=>{if(e.dataTransfer?.types.includes('Files')){e.preventDefault();form.classList.add('receiving-files')}});
form.addEventListener('dragleave',()=>form.classList.remove('receiving-files'));
form.addEventListener('drop',e=>{e.preventDefault();form.classList.remove('receiving-files');receiveFiles([...(e.dataTransfer?.files||[])])});
// Touch: hold and release to send. Mouse/keyboard: click to start, click again to send.
let voiceSession=null,voiceProcessing=false,lastVoiceTouch=0;
const voiceButton=$('voice'),voicePrompt=$('voice-prompt'),voicePromptText=$('voice-prompt-text');
function voiceMessage(text,cancel=false,processing=false){voicePromptText.textContent=text;voicePrompt.classList.toggle('cancel',cancel);voicePrompt.classList.toggle('processing',processing);voicePrompt.hidden=!text}
function stopVoiceTracks(session){if(session.stream)session.stream.getTracks().forEach(track=>track.stop())}
function finishVoice(session,cancel=false){
 if(!session||voiceSession!==session||session.finishing)return;
 session.held=false;session.cancel=session.cancel||cancel;session.finishing=true;clearTimeout(session.limitTimer);
 if(!session.recorder){voiceMessage('');return}
 if(session.recorder.state==='recording')session.recorder.stop();else{stopVoiceTracks(session);voiceSession=null;voiceButton.classList.remove('is-recording');voiceMessage('')}
}
async function deliverVoice(session,blob){
 if(session.cancel||Date.now()-session.startedAt<400){if(!session.cancel)showToast('说话时间太短，请按住后再说。');return}
 if(!blob.size){showToast('没有录到声音，请再试一次。');return}
 voiceProcessing=true;voiceButton.disabled=true;voiceMessage('正在识别语音…',false,true);uploading++;
 let uploaded=null,transcript='';
 try{
  const type=session.recorder.mimeType||blob.type||'audio/webm';const ext=type.includes('mp4')?'m4a':type.includes('ogg')?'ogg':'webm';
  const file=new File([blob],'语音-'+Date.now()+'.'+ext,{type});uploaded=await adapter.upload(file);
  const result=await adapter.transcribe(uploaded.id);transcript=String(result.text||'').trim();
  if(!transcript)throw new Error('empty_transcript');
 }catch(_){
  if(uploaded){attachments.push({id:uploaded.id,name:uploaded.name});renderAttachments();saveDraft()}
  showToast(uploaded?'这段语音暂时没听清，录音已保留在附件里。':'语音没有发送成功，请检查连接后重试。');
 }finally{uploading--;voiceProcessing=false;voiceButton.disabled=false;voiceMessage('')}
 if(!transcript)return;
 // Do not send any unrelated draft or attachment that was already in the composer.
 if(currentUser?.id!==session.userId||conversationId!==session.conversationId){
  attachments.push({id:uploaded.id,name:uploaded.name});query.value+=(query.value?'\n':'')+transcript;renderAttachments();resize();saveDraft();showToast('对话已切换，语音文字和录音已留在输入框。');return;
 }
 const until=Date.now()+30000;
 while(uploading&&Date.now()<until)await new Promise(resolve=>setTimeout(resolve,150));
 if(uploading||currentUser?.id!==session.userId||conversationId!==session.conversationId){
  attachments.push({id:uploaded.id,name:uploaded.name});query.value+=(query.value?'\n':'')+transcript;renderAttachments();resize();saveDraft();showToast('语音已留在输入框，可以继续发送。');return;
 }
 const savedText=query.value,savedFiles=attachments;query.value=transcript;attachments=[{id:uploaded.id,name:uploaded.name}];
 submit(transcript);query.value=savedText;attachments=savedFiles;renderAttachments();resize();saveDraft();
}
async function beginVoice(mode,event){
 if(!ready||voiceSession||voiceProcessing)return;
 if(!navigator.mediaDevices?.getUserMedia||typeof MediaRecorder==='undefined'){showToast('当前页面暂不能录音，请检查浏览器和麦克风权限。');return}
 const session={mode,held:true,cancel:false,finishing:false,stream:null,recorder:null,startedAt:0,userId:currentUser?.id,conversationId,startY:event?.clientY||0,pointerId:event?.pointerId};voiceSession=session;
 if(mode==='touch')try{voiceButton.setPointerCapture(session.pointerId)}catch(_){}
 voiceMessage('正在准备录音…');
 try{
  const stream=await navigator.mediaDevices.getUserMedia({audio:true});session.stream=stream;
  if(voiceSession!==session||session.finishing||!session.held){stopVoiceTracks(session);if(voiceSession===session)voiceSession=null;voiceMessage('');showToast(session.mode==='desktop'?'麦克风已就绪，请重新点击麦克风。':'麦克风已就绪，请重新按住说话。');return}
  const preferred=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'].find(t=>typeof MediaRecorder.isTypeSupported==='function'&&MediaRecorder.isTypeSupported(t));
  const rec=preferred?new MediaRecorder(stream,{mimeType:preferred}):new MediaRecorder(stream);const chunks=[];session.recorder=rec;
  rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
  rec.onerror=()=>{session.cancel=true;stopVoiceTracks(session);voiceButton.classList.remove('is-recording');voiceMessage('');if(voiceSession===session)voiceSession=null;showToast('录音中断了，请再试一次。')};
  rec.onstop=()=>{clearTimeout(session.limitTimer);stopVoiceTracks(session);voiceButton.classList.remove('is-recording');if(voiceSession===session)voiceSession=null;voiceMessage('');deliverVoice(session,new Blob(chunks,{type:rec.mimeType||'audio/webm'}))};
  rec.start();session.startedAt=Date.now();voiceButton.classList.add('is-recording');voiceMessage(mode==='touch'?'松开发送 · 上滑取消':'再次点击发送');
  session.limitTimer=setTimeout(()=>{if(voiceSession===session){showToast('录音已到一分钟，正在发送。');finishVoice(session)}},60000);
 }catch(err){stopVoiceTracks(session);if(voiceSession===session)voiceSession=null;voiceButton.classList.remove('is-recording');voiceMessage('');showToast(err?.name==='NotAllowedError'?'需要允许麦克风才能录音。':'暂时无法录音，请检查麦克风后重试。')}
}
voiceButton.addEventListener('pointerdown',event=>{if(event.pointerType!=='touch'&&event.pointerType!=='pen')return;event.preventDefault();lastVoiceTouch=Date.now();beginVoice('touch',event)});
window.addEventListener('pointermove',event=>{const s=voiceSession;if(!s||s.mode!=='touch'||s.pointerId!==event.pointerId||s.finishing)return;s.cancel=s.startY-event.clientY>60;voiceMessage(s.cancel?'松开取消':'松开发送 · 上滑取消',s.cancel)});
window.addEventListener('pointerup',event=>{const s=voiceSession;if(!s||s.mode!=='touch'||s.pointerId!==event.pointerId)return;event.preventDefault();lastVoiceTouch=Date.now();finishVoice(s)});
window.addEventListener('pointercancel',event=>{const s=voiceSession;if(s&&s.mode==='touch'&&s.pointerId===event.pointerId)finishVoice(s,true)});
voiceButton.addEventListener('click',event=>{if(Date.now()-lastVoiceTouch<800){event.preventDefault();return}if(voiceSession?.mode==='desktop')finishVoice(voiceSession);else beginVoice('desktop')});
document.addEventListener('visibilitychange',()=>{if(document.hidden&&voiceSession)finishVoice(voiceSession,true)});
$('add-context').addEventListener('click',()=>submit(query.value));

window.addEventListener('beforeunload',saveDraft);

query.disabled=true;send.disabled=true;resize();playIntro();init();
async function refreshBusinessSuggestions(){
 try{const data=await adapter.bootstrap();const items=data.suggestions;if(!Array.isArray(items))return;const parent=$('suggestions');parent.replaceChildren();for(const item of items){const b=create('button','suggest',item.label);b.type='button';b.dataset.ask=item.prompt;parent.append(b)}}catch(_){}
}
window.addEventListener('message',function(e){if(e.source!==window.parent||e.origin!==location.origin)return;const d=e.data||{};if(d.type==='jl-shell-opened'){try{query.blur();playIntro()}catch(_){}refreshBusinessSuggestions()}if(d.type==='jl-account-changed')refreshBusinessSuggestions()});
document.addEventListener('keydown',function(e){
 if(window.parent===window)return;
 if((e.ctrlKey||e.metaKey)&&e.shiftKey&&e.code==='Space'&&!e.repeat&&!e.isComposing){e.preventDefault();window.parent.postMessage({type:'jl-toggle-ai'},location.origin);}
 if(e.key==='Escape'&&!shade.classList.contains('open')&&!$('history-shade').classList.contains('open')&&!e.defaultPrevented){window.parent.postMessage({type:'jl-close-ai'},location.origin);}
},true);
(function(){const link=$('workbench-link');if(!link)return;link.addEventListener('click',function(e){if(window.parent!==window){e.preventDefault();try{window.parent.postMessage({type:'jl-close-ai'},'*')}catch(_){}}});})();
})();
