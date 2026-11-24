/* Shared app shell, live authenticated API; no demo data or timer-driven success. */
'use strict';
const live={boot:null,today:null,jobs:[],followups:[],history:[],notifications:[],conversation:null,messages:[],selected:null,detail:null,error:'',loading:true,attachmentIds:[],uploading:false,posting:false,pending:null,watch:null,poll:null,epoch:0,watchEpoch:0,scope:'enterprise',financeItems:[],financeReview:null,users:[],editingUser:null};
const activeStates=new Set(['queued','running']);
const labels={queued:'等待执行',running:'正在处理',succeeded:'已完成',failed:'未完成',cancelled:'已停止',interrupted:'服务中断',pending:'等待跟进',paused:'已暂停',done:'已完成'};
const roleNames={owner_finance:'企业负责人',dispatcher:'管理人员',employee:'业务员工'};
const arg=s=>esc(JSON.stringify(String(s)));
const arr=x=>Array.isArray(x)?x:[];
const textOf=x=>String(x??'');
const key=()=>Array.from(crypto.getRandomValues(new Uint8Array(16)),v=>v.toString(16).padStart(2,'0')).join('');
const stamp=s=>s?new Date(s).toLocaleString('zh-CN',{month:'numeric',day:'numeric',hour:'2-digit',minute:'2-digit'}):'';
async function api(path,body,headers={},method='POST'){
 const opt={credentials:'same-origin',headers:{Accept:'application/json',...headers}};
 if(body!==undefined){opt.method=method;if(body instanceof FormData)opt.body=body;else{opt.headers['Content-Type']='application/json';opt.body=JSON.stringify(body)}}
 let r;try{r=await fetch(path,opt)}catch(e){throw Error('网络连接失败，请检查连接后重试')}
 let d;try{d=await r.json()}catch(e){throw Error('服务返回异常')}
 if(!r.ok){let e=Error(d.message||d.error?.message||(typeof d.error==='string'?d.error:null)||('请求未完成（'+r.status+'）'));e.status=r.status;throw e}return d;
}
function fail(e){live.error=e.message||'操作未完成';if(e.status===401){resetSession();live.error='登录已过期，请重新登录'}render()}
function stopWatch(){live.watchEpoch++;if(live.watch)live.watch.close();clearInterval(live.poll);live.watch=null;live.poll=null}
function resetSession(){stopWatch();live.epoch++;Object.assign(live,{boot:null,today:null,jobs:[],followups:[],history:[],notifications:[],conversation:null,messages:[],selected:null,detail:null,attachmentIds:[],pending:null,posting:false,uploading:false,loading:false,scope:'enterprise',financeItems:[],financeReview:null,users:[],editingUser:null});modules.splice(0);view='home';closeSheet();$('prompt').value='';$('file').value=''}
function organization(){return live.boot?.organization?.name||'企业工作空间'}
function sidebar(){
 const org=esc(organization()),u=live.boot?.user||{};
 $('sidebar').innerHTML='<div class="brand"><span class="brandlogo">'+ic('mark')+'</span><div><b>工作空间</b><small>'+org+'</small></div></div>'+
 (live.boot?'<button class="new" onclick="go(\'new\')">'+ic('plus')+'新建工作</button><div class="nav">'+nav('home','工作台','home')+nav('clock','持续任务','tasks')+nav('file','工作记录','history')+'</div><div class="label">企业</div><div class="nav">'+nav('layers','业务中心','business')+'</div><div class="profile"><div class="avatar">'+esc(textOf(u.display_name).slice(0,1)||'企')+'</div><div class="grow"><b>'+esc(u.display_name||'已登录')+'</b><small>'+esc(roleNames[u.role]||u.role)+'</small></div><button class="iconbtn" onclick="logout()" aria-label="退出登录">×</button></div>':'');
}
function row(label,sub,action,icon='file'){return '<button class="taskrow" onclick="'+action+'">'+ic(icon)+'<span class="grow">'+esc(label)+'</span><small>'+esc(sub||'')+' ›</small></button>'}
function title(t,sub='',tag='企业工作空间'){return '<div class="intro appear"><div class="date">'+esc(tag)+'</div><h2>'+esc(t)+'</h2>'+(sub?'<p>'+esc(sub)+'</p>':'')+'</div>'}
function empty(t){return '<div class="live-empty">'+esc(t)+'</div>'}
function loginHTML(){return '<div class="empty appear">'+ic('spark')+'<h2>进入企业工作空间</h2><p>使用企业账号登录</p></div><form class="liveform" onsubmit="login(event)"><label>账号<input name="username" required autocomplete="username"></label><label>密码<input name="password" type="password" required autocomplete="current-password"></label><button class="primary" type="submit">登录</button></form>'}
async function login(e){e.preventDefault();const f=e.target,b=f.querySelector('button');b.disabled=true;try{const data=new FormData(f);await api('/api/login',{username:data.get('username'),password:data.get('password')});f.reset();await boot()}catch(err){fail(err)}finally{b.disabled=false}}
async function logout(){try{await api('/api/logout',{});resetSession();live.error='';render()}catch(e){fail(e)}}
async function boot(){live.loading=true;live.error='';render();try{live.boot=await api('/api/agent/ui/bootstrap');modules.splice(0,modules.length,...arr(live.boot.modules).filter(m=>typeof m.id==='string'&&/^[a-z0-9-]+$/.test(m.id)).map(m=>[m.id,Object.hasOwn(icons,m.icon)?m.icon:'file',m.label,m.description,m.prompt]));await refresh('home');await refresh('tasks');const running=live.jobs.find(j=>activeStates.has(j.state));if(running){live.selected=running;watch(running.id)}}catch(e){if(e.status===401){resetSession()}else{live.error=e.message}}finally{live.loading=false;render()}}
async function refresh(v){const epoch=live.epoch;let result;
 if(v==='home'){result=await api('/api/agent/today');if(epoch===live.epoch)live.today=result}
 if(v==='tasks'){const [a,b]=await Promise.all([api('/api/agent/jobs'),api('/api/agent/followups')]);if(epoch===live.epoch){live.jobs=arr(a.items);live.followups=arr(b.items)}}
 if(v==='history'){result=await api('/api/agent/conversations?memory_scope='+encodeURIComponent(live.scope)+'&limit=50');if(epoch===live.epoch)live.history=arr(result.items)}
 if(v==='home'||v==='tasks'){result=await api('/api/agent/notifications');if(epoch===live.epoch)live.notifications=arr(result.items)}
}
async function go(v){if(!live.boot)return;closeSheet();if(v==='new'){if(live.pending||live.uploading||live.posting){live.error='请先完成当前提交或上传';render();return}stopWatch();live.conversation=null;live.messages=[];live.selected=null;live.attachmentIds=[];$('prompt').value=''}view=v;live.error='';render();try{await refresh(v);if(view===v)render()}catch(e){fail(e)}}
function home(){
 const routine=live.today?.routine;
 if(!routine)return title('今日工作','正在读取企业业务状态…')+empty('等待服务返回');
 const candidates=arr(routine.routine_candidates);
 const groups=Object.entries(routine.by_routine||{});
 const gallery=groups.length?'<div class="gallery">'+groups.slice(0,3).map(([name,count],i)=>artifact(['a','b','c'][i],'alert',esc(name),'<h3>'+esc(name)+'</h3><div class="number">'+esc(count)+'</div><small>待核实事项</small>','查看明细','group:'+i)).join('')+'</div>':'';
 return title('今日工作',routine.total?'发现 '+routine.total+' 条待核实事项。':'当前扫描未返回待核实事项。','企业巡检')+gallery+
 '<p class="note livecopy">'+esc(routine.note||'')+'</p><div class="tasklist">'+candidates.map((c,i)=>row(c.kind||c.routine||'待核实事项',c.waybill_number||'', 'openSheet(\'case:'+i+'\',this)','alert')).join('')+'</div>'+
 (live.notifications.length?'<div class="label">消息</div>'+live.notifications.map((n,i)=>row(n.text,stamp(n.created_at),'openNotice('+i+')','clock')).join(''):'');
}
function accountEntry(){return ['owner_finance','dispatcher'].includes(live.boot?.user?.role)?row('企业账号管理','人员与权限','loadUsers()','layers'):''}
function businessHTML(){return title('业务中心','选择需要办理的业务。')+'<div class="business-grid appear">'+modules.map((m,i)=>'<button class="business-tile" onclick="openSheet(\'module:'+i+'\',this)"><span class="business-icon">'+ic(m[1])+'</span><span class="grow"><b>'+esc(m[2])+'</b><small>'+esc(m[3])+'</small></span><span class="business-arrow">›</span></button>').join('')+'</div>'+(modules.length?'':empty('暂无已开通的业务入口'))+accountEntry()}
function listHTML(){if(view==='history')return title('工作记录','从历史会话继续工作。')+scopeControls()+'<div class="tasklist">'+live.history.map((h,i)=>row(h.preview||'工作会话',stamp(h.updated_at),'openConversation('+arg(h.conversation_id)+')')).join('')+'</div>'+(live.history.length?'':empty('暂无工作记录'));
 return title('任务','正在执行的工作与持续跟进。')+'<div class="label">AI 工作</div><div class="tasklist">'+live.jobs.map(j=>row(j.title||'工作任务',labels[j.state]||j.state,'selectJob('+arg(j.id)+')','spark')).join('')+'</div>'+
 (live.jobs.length?'':empty('暂无 AI 工作'))+'<div class="label">持续跟进</div><div class="tasklist">'+live.followups.map((f,i)=>row(f.title,labels[f.state]||f.state,'openSheet(\'follow:'+i+'\',this)','clock')).join('')+'</div>'+(live.followups.length?'':empty('暂无持续任务'));
}
function scopeControls(){return '<div class="live-tools"><button class="secondary" '+(live.scope==='enterprise'?'disabled':'')+' onclick="changeScope(\'enterprise\')">企业工作</button><button class="secondary" '+(live.scope==='personal'?'disabled':'')+' onclick="changeScope(\'personal\')">个人工作</button></div>'}
async function changeScope(scope){if(live.pending||live.posting||live.uploading||activeStates.has(live.selected?.state)){live.error='当前工作尚未结束，请完成后切换记忆范围';render();return}stopWatch();live.scope=scope;live.conversation=null;live.messages=[];live.selected=null;live.attachmentIds=[];$('prompt').value='';await go(view==='history'?'history':'new')}
function moduleActions(m){let a='';if(m[0]==='finance')a+='<button class="secondary" onclick="loadFinance()">审阅费用与对账候选</button>';if((m[0]==='transport'||m[0]==='receipt')&&modules.some(x=>x[0]==='transport'))a+='<a class="secondary live-link" href="/manual-v9">人工录入与业务操作</a>';return a}
function sheetHTML(id){let [type,idx]=id.split(':'),i=Number(idx),heading='详情',body='',k='file';
 if(type==='module'){const m=modules[i];heading=m?.[2]||'业务';k=m?.[1]||'file';body=m?'<h2>'+esc(m[2])+'</h2><p>'+esc(m[3])+'</p><div class="sheetactions"><button class="primary" onclick="launch('+arg(m[4])+')">交给 AI 处理</button>'+moduleActions(m)+'</div>':empty('此入口未开通')}
 else if(type==='case'){const c=arr(live.today?.routine?.routine_candidates)[i];heading=c?.kind||'待核实事项';body=c?'<h2>'+esc(heading)+'</h2><p class="livecopy">'+esc(c.evidence||'')+'</p>'+field('业务编号',esc(c.waybill_number||'—'))+'<div class="sheetactions"><button class="primary" onclick="launch('+arg('调查以下待核实事项并整理证据：'+c.kind+' '+(c.waybill_number||''))+')">交给 AI 调查</button>'+(c.waybill_number?'<button class="secondary" onclick="loadCase('+arg(c.waybill_number)+')">查看记录</button>':'')+'</div>':empty('记录不存在')}
 else if(type==='group'){const g=Object.entries(live.today?.routine?.by_routine||{})[i];heading=g?.[0]||'事项';body='<h2>'+esc(heading)+'</h2>'+arr(live.today?.routine?.routine_candidates).map((c,n)=>c.routine===heading?row(c.kind,c.waybill_number,'openSheet(\'case:'+n+'\',this)'):'').join('')}
 else if(type==='follow'){const f=live.followups[i];heading='持续跟进';body=f?'<h2>'+esc(f.title)+'</h2>'+field('状态',esc(labels[f.state]||f.state))+field('计划时间',esc(stamp(f.due_at)))+'<div class="sheetactions">'+(['pending','paused'].includes(f.state)?'<button class="primary" onclick="manageFollow('+arg(f.id)+','+arg(f.state==='paused'?'resume':'pause')+')">'+(f.state==='paused'?'恢复':'暂停')+'</button><button class="secondary" onclick="manageFollow('+arg(f.id)+',\'cancel\')">取消跟进</button>':'')+'</div>':empty('任务不存在')}
 else if(type==='finance'){heading='财务确认';body=financeListHTML()}
 else if(type==='financeReview'){heading='审阅与确认';body=financeReviewHTML()}
 else if(type==='users'){heading='企业账号';body=usersHTML()}
 else if(type==='userEdit'){heading='账号设置';body=userEditHTML()}
 else if(type==='detail'){const d=live.detail;heading=d?.title||'业务记录';body=d?'<h2>'+esc(heading)+'</h2>'+arr(d.groups).map(g=>'<div class="label">'+esc(g.title)+'</div>'+arr(g.rows).map(r=>field(esc(r.label),esc(r.value))).join('')).join('')+'<p>'+esc(d.note||'')+'</p>':empty('无记录')}
 else{heading='工作空间';k='menu';body=['home','tasks','business','history'].map((v,n)=>row(['工作台','持续任务','业务中心','工作记录'][n],'','go('+arg(v)+')')).join('')+'<button class="secondary" onclick="logout()">退出登录</button>'}
 return '<div class="sheethead"><span class="handle" aria-hidden="true"></span>'+ic(k)+'<span class="grow">'+esc(heading)+'</span><button id="sheetClose" class="iconbtn" onclick="closeSheet()" aria-label="关闭">'+ic('x')+'</button></div><div class="sheetbody">'+body+'</div>';
}
async function loadCase(id){try{live.detail=await api('/api/agent/cases/'+encodeURIComponent(id));openSheet('detail')}catch(e){fail(e)}}
async function manageFollow(id,action){try{await api('/api/agent/followups',{id,action});closeSheet();await refresh('tasks');render();notify('跟进任务已更新')}catch(e){fail(e)}}
async function openNotice(i){const n=live.notifications[i];if(!n)return;try{await api('/api/agent/notifications/seen',{id:n.id});if(n.conversation_id)await openConversation(n.conversation_id);else await go('tasks')}catch(e){fail(e)}}
function messageHTML(m){const b=arr(m.blocks).map(x=>x.type==='case'?'<button class="secondary" onclick="loadCase('+arg(x.case_id)+')">'+esc(x.label||'查看记录')+'</button>':x.type==='file'?'<a href="/api/agent/files/'+encodeURIComponent(x.file_id)+'" target="_blank" rel="noopener">'+esc(x.label||'附件')+'</a>':'').join('');return '<div class="'+(m.role==='user'?'userbubble':'result')+' livecopy">'+esc(m.text||'')+(b?'<div class="live-tools live-result">'+b+'</div>':'')+'</div>'}
async function openConversation(id){if(live.posting||live.pending||live.uploading)return;const epoch=live.epoch;stopWatch();try{const d=await api('/api/agent/conversations/'+encodeURIComponent(id));if(epoch!==live.epoch)return;live.scope=d.memory_scope||'enterprise';live.conversation=d.redacted?null:id;live.messages=arr(d.messages);live.selected=null;live.attachmentIds=[];view='job';render()}catch(e){if(epoch===live.epoch)fail(e)}}
function jobHTML(){const j=live.selected;let body=live.messages.map(messageHTML).join('');
 if(j){body+='<div class="jobbox appear"><div class="jobhead"><div class="jobmotion '+(activeStates.has(j.state)?'running':'')+'">'+ic(j.state==='succeeded'?'check':'spark')+'</div><div class="grow"><h3>'+esc(labels[j.state]||j.state)+'</h3><small>'+esc(j.progress||'')+'</small></div></div>'+(activeStates.has(j.state)?'<div class="track indeterminate"><div></div></div><div class="jobcontrols"><button class="secondary" '+(j.cancel_requested?'disabled':'')+' onclick="toggleJob()">'+(j.cancel_requested?'正在停止…':'停止后续工作')+'</button></div>':'')+'</div>';
 if(j.state==='succeeded'&&j.result?.message&&!live.messages.some(m=>m.id===j.result.message.id||(m.role==='assistant'&&m.text===j.result.message.text)))body+=messageHTML(j.result.message);
 if(['failed','interrupted','cancelled'].includes(j.state))body+='<p class="note">'+esc(j.result?.message||j.progress||'任务未完成，已完成的业务动作可能保留。')+'</p>'}
 return body||empty('暂无工作内容');
}
function updateStatus(){const j=live.selected;let running=j&&activeStates.has(j.state);$('canvas').classList.toggle('busy',!!running);$('statusdot').className='statusdot'+(running?' busy':j?.state==='succeeded'?' done':'');$('status').textContent=running?'正在执行':live.boot?'工作就绪':'尚未登录'}
function render(){
 $('app').dataset.view=view;$('content').dataset.view=view;sidebar();renderTabs();$('organizationName').textContent=organization();$('title').textContent={home:'工作空间',new:'新建工作',job:'工作任务',tasks:'持续任务',history:'工作记录',business:'业务中心'}[view]||'工作空间';
 $('content').innerHTML=(live.error?'<p class="live-error" role="alert">'+esc(live.error)+'</p>':'')+(live.pending&&!live.posting?'<button class="secondary" onclick="retryPending()">重试上一条提交</button>':'')+(!live.boot?(live.loading?empty('正在连接企业服务…'):loginHTML()):view==='home'?home():view==='business'?businessHTML():view==='new'?title('今天，需要我做什么？','直接交代工作，或添加相关资料。')+scopeControls():view==='job'?jobHTML():listHTML());
 $('suggestions').innerHTML=live.boot&&(view==='home'||view==='new')?'<button onclick="go(\'new\')">'+ic('plus')+'新建工作</button>':'';
 $('prompt').disabled=!live.boot||live.posting;$('sendBtn').disabled=!live.boot||live.posting||live.uploading;$('attachBtn').disabled=!live.boot||live.uploading;$('newBtn').disabled=!live.boot;$('historyBtn').disabled=!live.boot;$('menuBtn').disabled=!live.boot;
 $('fine').textContent=live.boot?(live.uploading?'正在上传资料…':live.attachmentIds.length?'已上传 '+live.attachmentIds.length+' 份资料，发送后交由 AI 处理':'企业授权范围内执行'):'请先登录企业账号';
 updateStatus();
}
async function launch(t){if(!live.boot||live.posting)return;closeSheet();let p=live.pending;if(p&&(p.body.text!==t||p.body.conversation_id!==(live.conversation||undefined))){live.error='上一条提交结果尚未确认，请先重试原任务，避免重复操作。';render();return}
 if(!p){const body={text:t,memory_scope:live.scope,attachment_ids:[...live.attachmentIds]};if(live.conversation)body.conversation_id=live.conversation;p=live.pending={body,key:key()}}
 const epoch=live.epoch;live.posting=true;live.error='';render();try{let d=await api('/api/agent/jobs',p.body,{'Idempotency-Key':p.key});if(epoch!==live.epoch)return;live.pending=null;live.attachmentIds=[];live.conversation=d.conversation_id;live.messages.push({role:'user',text:t});$('prompt').value='';view='job';live.selected={id:d.job_id,state:'queued',progress:'等待服务接收'};watch(d.job_id);render()}catch(e){if(epoch===live.epoch){if(e.status&&e.status<500)live.pending=null;fail(e)}}finally{if(epoch===live.epoch){live.posting=false;render()}}}
function retryPending(){if(live.pending){live.scope=live.pending.body.memory_scope;live.conversation=live.pending.body.conversation_id||null;launch(live.pending.body.text)}}
function send(e){e.preventDefault();const t=$('prompt').value.trim();if(t)launch(t)}
async function attach(el){if(!el.files.length)return;const epoch=live.epoch;live.uploading=true;render();try{for(const f of Array.from(el.files)){if(epoch!==live.epoch)return;const form=new FormData();form.append('file',f);const d=await api('/api/agent/files',form);if(epoch===live.epoch&&d.file?.id)live.attachmentIds.push(d.file.id)} }catch(e){if(epoch===live.epoch)fail(e)}finally{if(epoch===live.epoch){el.value='';live.uploading=false;render()}}}
async function selectJob(id){if(live.posting||live.pending||live.uploading)return;const epoch=live.epoch;stopWatch();try{const j=await api('/api/agent/jobs/'+encodeURIComponent(id));if(epoch!==live.epoch)return;live.selected=j;live.conversation=j.conversation_id||null;live.messages=[];live.attachmentIds=[];if(live.conversation){const d=await api('/api/agent/conversations/'+encodeURIComponent(live.conversation));if(epoch!==live.epoch)return;live.scope=d.memory_scope||'enterprise';live.messages=arr(d.messages);if(d.redacted)live.conversation=null}view='job';if(activeStates.has(j.state))watch(id);render()}catch(e){if(epoch===live.epoch)fail(e)}}
function watch(id){
 stopWatch();
 const epoch=live.epoch,watchEpoch=live.watchEpoch;
 let last='',busy=false,streamHealthy=false;
 const valid=()=>epoch===live.epoch&&watchEpoch===live.watchEpoch;
 function accept(j){
  if(!valid()||j.id!==id)return;
  const sig=JSON.stringify(j);if(sig===last)return;last=sig;
  live.jobs=[j,...live.jobs.filter(x=>x.id!==id)];
  if(live.selected?.id===id){live.selected=j;if(view==='job')render();else updateStatus()}
  if(!activeStates.has(j.state)){stopWatch();if(j.state==='succeeded')notify('本次 Agent 执行已结束，请查看实际结果');}
 }
 async function check(){
  if(!valid()||busy||streamHealthy)return;
  busy=true;
  try{accept(await api('/api/agent/jobs/'+encodeURIComponent(id)))}catch(e){if(valid()&&e.status===401){stopWatch();fail(e)}}finally{busy=false}
 }
 if(typeof EventSource!=='undefined'){
  live.watch=new EventSource('/api/agent/jobs/'+encodeURIComponent(id)+'/events');
  live.watch.onmessage=e=>{if(!valid())return;try{streamHealthy=true;accept(JSON.parse(e.data))}catch(_){streamHealthy=false}};
  live.watch.onerror=()=>{streamHealthy=false;check()};
 }
 live.poll=setInterval(check,3000);
 check();
}
async function toggleJob(){const j=live.selected;if(!j||!activeStates.has(j.state))return;try{live.selected=await api('/api/agent/jobs/'+encodeURIComponent(j.id)+'/cancel',{});render()}catch(e){fail(e)}}
async function loadFinance(){if(!live.boot?.capabilities?.financial_analysis)return;const epoch=live.epoch;try{const d=await api('/api/finance/candidates');if(epoch!==live.epoch)return;live.financeItems=arr(d.items);openSheet('finance')}catch(e){if(epoch===live.epoch)fail(e)}}
function financeListHTML(){return '<h2>待人工确认</h2><p>候选由业务流程生成，确认前不会修改账本。</p>'+live.financeItems.map(c=>row(c.kind==='difference'?'对账差异':'费用确认',c.record_id||'', 'loadFinanceReview('+arg(c.candidate_id)+')','money')).join('')+(live.financeItems.length?'':empty('暂无有效待确认候选'))}
async function loadFinanceReview(id){const epoch=live.epoch;try{const d=await api('/api/finance/candidates/'+encodeURIComponent(id));if(epoch!==live.epoch)return;live.financeReview=d;openSheet('financeReview')}catch(e){if(epoch===live.epoch)fail(e)}}
function evidenceFields(obj){return Object.entries(obj||{}).map(([k,v])=>field(esc(k),esc(typeof v==='object'?JSON.stringify(v):textOf(v)))).join('')}
function financeReviewHTML(){const d=live.financeReview;if(!d)return empty('候选不存在');return '<h2>核对业务凭证</h2>'+evidenceFields(d.fee||d.proposal||{})+(d.difference?evidenceFields(d.difference):'')+arr(d.fees).map(evidenceFields).join('')+arr(d.bills).map(evidenceFields).join('')+field('目标状态',esc(d.target_status||d.proposal?.decision||'—'))+field('账本版本',esc(d.expected_rev))+(d.reason?field('原因',esc(d.reason)):'')+(d.stale||d.completed?empty(d.completed?'已经确认':'账本已变化，请重新生成候选'):'<form class="liveform" onsubmit="confirmFinance(event)"><label>重新输入当前账号密码<input name="password" type="password" required autocomplete="current-password"></label><button type="submit" class="primary">确认并写入账本</button><p class="live-error" data-form-error role="alert"></p></form>')}
async function confirmFinance(e){e.preventDefault();const f=e.currentTarget,b=f.querySelector('button'),d=live.financeReview,epoch=live.epoch;if(!d||d.stale||d.completed||b.disabled)return;b.disabled=true;try{await api('/api/finance/candidates/'+encodeURIComponent(d.candidate_id)+'/confirm',{password:new FormData(f).get('password')});if(epoch!==live.epoch)return;f.reset();await loadFinance();notify('财务操作已确认')}catch(err){if(epoch===live.epoch)f.querySelector('[data-form-error]').textContent=err.status===403?'密码错误或没有确认权限':err.message}finally{f.reset();b.disabled=false}}
async function loadUsers(){const epoch=live.epoch;try{const d=await api('/api/users');if(epoch!==live.epoch)return;live.users=arr(d.items);openSheet('users')}catch(e){if(epoch===live.epoch)fail(e)}}
function usersHTML(){return '<h2>企业人员</h2>'+live.users.map((u,i)=>row(u.name+' · '+u.username,(roleNames[u.role]||u.role)+(u.active?'':' · 停用'),'editUser('+i+')','layers')).join('')+'<button class="primary" onclick="editUser(-1)">新增账号</button>'}
function editUser(i){live.editingUser=i<0?null:live.users[i];openSheet('userEdit')}
function roleOptions(selected){const roles=live.boot.user.role==='owner_finance'?['employee','dispatcher','owner_finance']:['employee'];return roles.map(r=>'<option value="'+r+'" '+(r===selected?'selected':'')+'>'+roleNames[r]+'</option>').join('')}
function userEditHTML(){const u=live.editingUser,owner=live.boot.user.role==='owner_finance';if(!u)return '<h2>新增账号</h2><form class="liveform" onsubmit="saveUser(event)"><label>账号<input name="username" required minlength="3"></label><label>名称<input name="name" required></label><label>角色<select name="role">'+roleOptions('employee')+'</select></label><label>初始密码<input name="password" required minlength="12" type="password" autocomplete="new-password"></label><button class="primary">创建账号</button><p class="live-error" data-form-error></p></form>';return '<h2>'+esc(u.name)+'</h2><form class="liveform" onsubmit="saveUser(event)">'+(owner?'<label>名称<input name="name" value="'+esc(u.name)+'" required></label><label>角色<select name="role">'+roleOptions(u.role)+'</select></label>':'')+'<label>账号状态<select name="active"><option value="true" '+(u.active?'selected':'')+'>启用</option><option value="false" '+(!u.active?'selected':'')+'>停用</option></select></label><button class="primary">保存账号设置</button><p class="live-error" data-form-error></p></form><form class="liveform" onsubmit="resetUserPassword(event)"><label>新密码<input name="password" required minlength="12" type="password" autocomplete="new-password"></label><button class="secondary">重设密码并注销旧会话</button><p class="live-error" data-form-error></p></form>'}
async function saveUser(e){e.preventDefault();const f=e.currentTarget,b=f.querySelector('button'),data=Object.fromEntries(new FormData(f)),u=live.editingUser,epoch=live.epoch;if(b.disabled)return;b.disabled=true;try{if(u){data.active=data.active==='true';await api('/api/users/'+encodeURIComponent(u.id),data,{},'PATCH')}else await api('/api/users',data);if(epoch!==live.epoch)return;f.reset();await loadUsers();notify('账号设置已保存')}catch(err){if(epoch===live.epoch)f.querySelector('[data-form-error]').textContent=err.message}finally{b.disabled=false;const p=f.querySelector('[name=password]');if(p)p.value=''}}
async function resetUserPassword(e){e.preventDefault();const f=e.currentTarget,b=f.querySelector('button'),u=live.editingUser,epoch=live.epoch;if(!u||b.disabled)return;b.disabled=true;try{await api('/api/users/'+encodeURIComponent(u.id)+'/password',{password:new FormData(f).get('password')});if(epoch!==live.epoch)return;if(u.id===live.boot.user.id){resetSession();render()}else{await loadUsers();notify('密码已更新，旧会话已撤销')}}catch(err){if(epoch===live.epoch)f.querySelector('[data-form-error]').textContent=err.message}finally{f.reset();b.disabled=false}}
boot();
