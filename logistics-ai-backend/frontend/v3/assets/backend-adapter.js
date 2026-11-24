(function(){
'use strict';
// Live integration boundary for the existing authenticated WorkBuddy API.
const BASE=(window.JL_AGENT_API_BASE||'').replace(/\/$/,'');
let capabilities={};
async function request(path,{method='GET',body,form=false,headers={}}={}){
 const options={method,credentials:'same-origin',headers:{'Accept':'application/json',...headers}};
 if(body!==undefined){if(form){options.body=body}else{options.headers['Content-Type']='application/json';options.body=JSON.stringify(body)}}
 const response=await fetch(BASE+path,options);let data={};
 try{data=await response.json()}catch(_){ }
 if(!response.ok){const detail=data.error&&typeof data.error==='object'?data.error.message:data.error;const err=new Error(data.message||detail||('业务服务暂不可用（'+response.status+'）'));err.status=response.status;err.payload=data;throw err}
 return data;
}
window.JLBackendAdapter={
 async bootstrap(){const data=await request('/api/agent/ui/bootstrap');capabilities=data.capabilities||{};const user=data.user||{};return {...data,user:{...user,display_name:user.display_name||user.name||user.username||'已登录用户',salutation:user.salutation||user.display_name||user.name||user.username||''},organization:data.organization||{name:'巨龙物流'},workbench_url:data.workbench_url||'/manual-v9',capabilities:{...(data.capabilities||{})}}},
 async login(username,password){return request('/api/login',{method:'POST',body:{username,password}})},
 async logout(){return request('/api/logout',{method:'POST',body:{}})},
 createConversation(){return request('/api/agent/conversations',{method:'POST',body:{memory_scope:'enterprise'}})},
 async listConversations({limit=30,offset=0}={}){return request(`/api/agent/conversations?memory_scope=enterprise&limit=${limit}&offset=${offset}`)},
 async getConversation(id){const result=await request('/api/agent/conversations/'+encodeURIComponent(id));const conversation=result.conversation||result;const messages=(conversation.messages||[]).map(message=>({role:message.role==='system'?'assistant':message.role,text:message.text||message.content||'',created_at:message.created_at,redacted:!!message.redacted,blocks:Array.isArray(message.blocks)?message.blocks:[]}));return {...conversation,messages,redacted:!!conversation.redacted||messages.some(message=>message.redacted)}},
 async send({conversation_id,text,attachment_ids=[],onProgress,jobKey,onJob}){let context=null;
 try {if(window.parent!==window && window.parent.location.origin===location.origin && window.parent.__JL_SHELL)context=window.parent.__JL_SHELL.context();}catch(_){}
 const number=context&&String(context.number||'');
 const view=context&&context.view;
 const validView=['查看运单','编辑运单','录入运单（尚未保存）'].includes(view);
 const contextualText=number&&/^\d{1,32}$/.test(number)&&validView ? text+'\n\n（当前手动台：'+view+' '+number+'。这是当前页面定位，实际业务信息以授权查询结果为准。）' : text;
 const body={conversation_id,text:contextualText||'请查看附件',memory_scope:'enterprise',attachment_ids};
 if(!capabilities.durable_jobs)return request('/api/agent/chat',{method:'POST',body});
 const started=await request('/api/agent/jobs',{method:'POST',body,headers:{'Idempotency-Key':jobKey||crypto.randomUUID()}}).catch(err=>{throw err});
 if(onJob)onJob(started);
 return window.JLBackendAdapter.watchJob(started.job_id,onProgress);
 },
 async watchJob(id,onProgress){
  return new Promise((resolve,reject)=>{
   let stream=null,poll=null,ended=false;
   const done=()=>{ended=true;if(stream)stream.close();clearInterval(poll)};
   const accept=data=>{if(ended)return;if(onProgress)onProgress(data);if(['queued','running'].includes(data.state))return;done();if(data.state==='succeeded')resolve(data.result);else {const e=new Error(data.result?.message||(data.state==='cancelled'?'已停止后续处理，原资料和已完成操作仍保留。':'这次没有完成，资料已保留，可以继续。'));e.state=data.state;reject(e)}};
   stream=new EventSource(BASE+'/api/agent/jobs/'+encodeURIComponent(id)+'/events');
   stream.onmessage=e=>{try{accept(JSON.parse(e.data))}catch(_){}};
   const check=async()=>{try{accept(await request('/api/agent/jobs/'+encodeURIComponent(id)))}catch(e){if(e.status===401||e.status===404){done();reject(e)}}};
   stream.onerror=check;poll=setInterval(check,3000);check();
  });
 },
 cancelJob(id){return request('/api/agent/jobs/'+encodeURIComponent(id)+'/cancel',{method:'POST',body:{}})},
 jobs(){return request('/api/agent/jobs')},
 usage(scope='mine'){return request('/api/agent/usage?scope='+encodeURIComponent(scope))},
 health(){return request('/api/agent/health')},
 eventAlerts(){return request('/api/agent/event-alerts')},
 setEventAlerts(enabled){return request('/api/agent/event-alerts',{method:'POST',body:{enabled},headers:{'Idempotency-Key':crypto.randomUUID()}})},
 notifications(){return request('/api/agent/notifications')},
 today(){return request('/api/agent/today')},
 routineScan(id){return request('/api/agent/routine-scans/'+encodeURIComponent(id))},
 seen(id){return request('/api/agent/notifications/seen',{method:'POST',body:{id}})},
 followups(){return request('/api/agent/followups')},
 manageFollowup(action,id,fields={}){return request('/api/agent/followups',{method:'POST',body:{action,...(id?{id}:{}),...fields}})},
 transcribe(id){return request('/api/agent/transcribe',{method:'POST',body:{file_id:id}})},

 getCase(id){return request('/api/agent/cases/'+encodeURIComponent(id))},
 async upload(file){const form=new FormData();form.append('file',file);const result=await request('/api/agent/files',{method:'POST',body:form,form:true});return {...result.file,name:result.file.filename}}
};
})();
