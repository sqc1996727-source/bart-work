"""Small on-demand skill catalog; instructions are not conversation templates."""
from pathlib import Path
SKILLS={
 'team':('团队交接与公司资料','业务负责人、协作人、共享原件、资料版本、实际导入、事件提醒和用量查询。'),
 'files':('资料阅读与证据定位','多份照片、Excel、PDF、Word的阅读与整理，保留原件引用。'),
 'analysis':('表格核对与成果交付','跨文件比较、保留单号与金额原值、生成表格/PDF/资料包。'),
 'lookup':('模糊查找业务','按人物、到站、时间和别名检索候选，不把近似结果当已确认归属。'),
 'memory':('记忆与企业规则','按授权范围查找、记住和更正个人偏好、企业经验及规则。'),
 'followup':('持续跟进与协作','设定提醒、检查回单或任务、完成后回报当前用户。')}

def schema(name,description,properties,required=()):
 return {'type':'function','function':{'name':name,'description':description,'parameters':{'type':'object','properties':properties,'required':list(required),'additionalProperties':False}}}

def tools():
 string={'type':'string'};ids={'type':'array','items':{'type':'string'},'maxItems':12}
 return [
 schema('list_capabilities','查看当前可使用的专业能力及技能目录；按需要选择，不必每轮调用。',{}),
 schema('read_skill','按需阅读一个技能的方法和工具用法，技能不规定固定话术或机械步骤。',{'name':{'type':'string','enum':list(SKILLS)}},['name']),
 schema('read_files','阅读当前用户有权限的上传或共享资料，返回文字、表格、页码/坐标/时间戳；输入仅文件ID，不接受URL或磁盘路径。',{'file_ids':ids,'text_offset':{'type':'integer','minimum':0},'sheet_index':{'type':'integer','minimum':0},'row_offset':{'type':'integer','minimum':0}},['file_ids']),
 schema('analyze_table','对来源表格做精确十进制汇总、重复单号检查或两表对比，返回原行依据；不会自动改账。列和工作表序号从0开始，行号从1开始；先读表头选列。',{'action':{'type':'string','enum':['summarize','duplicates','reconcile']},'file_ids':ids,'sheet_indices':{'type':'array','items':{'type':'integer','minimum':0},'maxItems':2},'header_rows':{'type':'integer','minimum':0,'maximum':10},'key_column':{'type':'integer','minimum':0,'maximum':79},'amount_column':{'type':'integer','minimum':0,'maximum':79}},['action','file_ids']),
 schema('find_records','按当前授权范围模糊找运单、自己上传的文件和历史原话；用于不记得单号、名字或具体说法时寻找候选，之后自主核对。',{'query':string,'kind':{'type':'string','enum':['all','waybills','files','conversations']},'limit':{'type':'integer','minimum':1,'maximum':20}},['query']),
 schema('recall_work_result','按需找回本人同记忆范围、仍有文件权限的历史文件分析或交付原始结果。先 query 查候选，再 event_id 读取；历史结果不代表现在的业务状态。',{'query':string,'event_id':string,'offset':{'type':'integer','minimum':0}},[]),
 schema('create_document','生成当前用户可下载的分析表格、PDF或资料包；来源文件须在当前授权范围内。正式巨龙打印格式继续使用原业务导出。',{'format':{'type':'string','enum':['xlsx','csv','pdf','zip']},'title':string,'file_ids':ids,'text':string,'columns':{'type':'array','items':string,'maxItems':40},'rows':{'type':'array','items':{'type':'array','items':{'type':['string','number','boolean','null']}},'maxItems':1000}},['format','title']),
 schema('manage_preference','仅在用户明确提供个人称呼/习惯/偏好时保存、更正或撤回；不保存凭据、财务账副本或临时推测。',{'action':{'type':'string','enum':['remember','forget','list']},'key':string,'value':string},['action']),
 schema('search_workplace_memory','跨行业查本人偏好、企业规则、经验和待核实线索；近似结果是候选，需核对适用范围、来源、有效期及冲突。',{'query':string},['query']),
 schema('save_workplace_memory','记录个人偏好，或保存企业经验、线索和规则。action 必须显式传，不传直接拒绝（不再按 kind 推断）：主动沉淀被纠正的结论、干成的活、新发现的口径用 action=note（无需 expected_version，applies_to 可选；kind 只限 preference/experience/lead）；正式规则才用 action=save 并先搜索同主题确定 expected_version；撤回用 action=forget，只需 scope 和 topic；save/note 需 kind/title/content。未经确认的信息只能记作线索，不能升级成规则。个人隐私不要共享；不保存密码、密钥、凭证明文或业务账副本；涉钱只记结论不记原文明细。',{ 'action':{'type':'string','enum':['save','forget','note']},'scope':{'type':'string','enum':['personal','company']},'topic':string,'expected_version':{'type':'integer','minimum':0},'kind':{'type':'string','enum':['preference','experience','lead','rule']},'title':string,'content':string,'applies_to':string,'valid_from':string,'valid_until':string,'audience':{'type':'string','enum':['employee','dispatcher','owner_finance']}},['action','scope','topic']),
 schema('schedule_followup','为当前用户建立一次提醒、持续跟进，或一个「定时干活」的循环任务。普通提醒/跟进到点在原会话回报；设定时干活用 condition=run_routine + schedule 表达式（interval:N 每N分钟 / daily:HH:MM 每天 / weekdays:HH:MM 工作日），到点自动扫账并把结果回报。仅当用户明确交办定时执行任意业务目标时，使用 condition=run_agent 和 agent_instruction（可一次执行或按 schedule 重复）；按当前权限真实执行，回原会话报告，禁止定时工作再派生定时任务。',{'title':string,'due_at':string,'condition':{'type':'string','enum':['remind','paper_received','receipt_delivered','task_done','run_routine','run_agent']},'waybill_number':string,'task_id':string,'every_minutes':{'type':'integer','minimum':1,'maximum':1440},'schedule':string,'routine_kind':{'type':'string','enum':['daily_scan']},'agent_instruction':{'type':'string','maxLength':6000}},['title','due_at']),
 schema('update_fee_status','生成费用状态变更候选，不会修改费用；必须由授权人员在专用界面审阅并重新认证。fee_id 是费用的唯一编号，status 取 已确认/有差异/待确认。',{'fee_id':string,'status':{'type':'string','enum':['已确认','有差异','待确认']}},['fee_id','status']),
 schema('send_message','给同企业指定同事发一条站内消息。员工只接收固定提醒：请查看回单、请核对运单、请查看工作台待办。to_user_id 是对方用户 id。',{'to_user_id':string,'text':{'type':'string','maxLength':1000}},['to_user_id','text']),
 schema('manage_followups','查看或取消当前用户创建的跟进任务。',{'action':{'type':'string','enum':['list','cancel']},'id':string},['action'])]

def read(name):
 if name not in SKILLS:raise ValueError('unknown_skill')
 return {'name':name,'instructions':(Path(__file__).parent/'skills'/(name+'.md')).read_text()}
