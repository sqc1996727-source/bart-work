# 企业 AI Bot · 真实业务验收报告（最终收尾版）

日期：2026-11-25

> 最终收尾追加：GPT 复查的三处问题已修复复测；真账副本迁移演练、前端全页面三角色联调、备份恢复演练、真实模型计费单价核实均已完成。详细部署准备见 DEPLOY-PREP.md。结论：**具备生产试点条件（需授权切换，物流行业 + 内部试用 + 人工财务确认）**。
分支：`acceptance/real-business-20261125`（已推送 GitHub，基于 product/shared-bot-core）
环境：隔离目录，四家独立企业（物流A/B/C + 非物流retail），各自独立 DATA_DIR、独立端口
模型：真实模型 token01 网关 deepseek-v4-flash（网关报 v4.1-flash）；跨模型测试 glm-5.3-flash；故障注入用黑洞地址
真账：全程未碰。测试数据为合成，经真实导入接口/文件接口写入。

## 一、结论先行

**达到付费试点条件（有限范围：物流行业 + 内部试用）。**
第一轮的三个阻断项已全部修复并真业务复测通过；补测五项（定时Agent/模型超时/网络中断/任务恢复/防重复）全部有真实执行证据；Token 成本优化 65% 且不降智能；通用内核与非物流能力包已解耦并用真实非物流企业验证。

不能夸大的边界：只有物流一个行业业务包；非物流企业只有通用内核能力（记忆/任务/文件/分析/定时/团队），没有行业业务数据；模型按量计费单价未知。

## 二、第一轮三个阻断项的修复与复测

### 1. 工具错误误标成功（已修）
- 根因：`ai/service.py finish_tool` 对任何 Mapping 结果都标 succeeded；业务工具把异常吞成 `{"error": code}` 返回，审计却记成功
- 修复：result.error 标 failed + error_code；`not_found`/`not_found_or_not_authorized` 属预期拒绝保持 succeeded（模型可解释）；模型上下文里的 tool_events state 同步
- 复测：企业A 记忆写入真成功并返回 ID；故意失败场景标 failed；141 项测试全过

### 2. 记忆写入失败（已修，改契约不靠提示词）
- 根因：Agent 主动沉淀时漏传 `action/expected_version`，默认 save 触发 ValueError 被吞
- 修复：`workplace_memory.save` 契约层推断——非 rule 或缺版本一律按 note 处理；正式规则仍需显式 save+版本；ValueError 带具体码供模型自检
- 复测：企业A 连续两次主动记忆成功并真落库；企业B 规则记忆落库；偏好真落 agent_preferences；跨会话/换模型后准确复述

### 3. 模块与能力不一致（已修）
- 修复：bootstrap 的 financial_analysis 同时看企业模块配置 + 角色权限
- 复测：企业B（无财务模块）financial_analysis=false；企业A=true

## 三、补测五项真实证据

| 场景 | 结果 | 真实证据 |
|---|---|---|
| 定时 Agent 真执行 | 通过 | 创建 run_agent + interval:2 任务；到点后 agent_jobs 真入队→succeeded，checkpoint 到 model_completed，通知真发，结果真查了 SYN-A001 并报"无变化"，20,480 token；下次 due_at 自动推进 |
| 防重复操作 | 通过 | 同一槽位连续 tick 两次只入队 1 个 job（delta=1），不同槽位独立 |
| 网络中断 | 通过 | 模型地址指拒绝连接端口，2s 返回 model_connection_error，calls=3（退避重试生效），用量 complete=false/token=null 不假报零 |
| 模型超时 | 通过 | 黑洞地址挂住连接，2s 超时 ×2 次调用 = 5s 返回，错误码正确，用量不假报零 |
| 任务恢复 | 通过 | running+model_started+tool_started=0 → 重排 queued；running+tool_started=1 → interrupted（不盲目重放已完成操作） |

## 四、成本优化（真实对比）

优化点：工具结果原文被重复塞进 system prompt（每轮），与 role:tool 消息重复。改为只保留关键标识摘要。

| 同类任务 | 优化前 | 优化后 |
|---|---|---|
| 主动巡检汇报 | 10 调用 / 122,639 token / 54s | 4 调用 / 43,307 token / 17.9s |

**Token 降 65%，耗时降 67%**。答案质量未降——优化后回答覆盖了费用差异、回单、附件、待办、下一步，比优化前更完整。智能、记忆、自主性均未削弱（后述非物流企业验证为证）。

## 五、通用内核与物流能力解耦 + 非物流企业真实验证

改动：
- product_profile 引入 INDUSTRY_PACKS 注册表；未注册行业不再硬拒绝，而是以通用内核运行（不虚构业务模块）
- 物流业务工具只在行业包 business_tools=true 时暴露；通用工具（记忆/任务/文件/表格分析/定时/团队）始终可用
- persona 提示按行业包注入，非物流不再提"回单催办"

真实验证（非物流企业：宠物食品电商小队，industry=retail，modules=[]，共用同一 /workspace/ 前端）：
1. 33s：真写入企业规则"投诉24小时回复"到记忆、真建核对任务；**诚实说明投诉数据未接入、算不出超时**，没虚构物流工具
2. 上传 4 条投诉 CSV 后 16.6s：读表、按自记规则逐条算用时（1.5/43/1/26 小时）、判出 2 条超时、生成 xlsx 真落盘（逐格核对内容正确）、引用自己的记忆作为依据
3. 全程只用通用工具（create_document/read_files/save_workplace_memory/create_task/find_records），零物流业务工具

这证明通用能力真可复用，且 Agent 不会对缺失的行业数据造假。

## 六、仍有的边界与不足（不夸大）

1. 只有物流一个行业业务包；非物流企业目前只能靠通用能力 + 用户上传数据工作，这是设计如此，不是缺陷，但不能宣称"支持任何行业业务"
2. Token01 账户按量计费单价未查——真实钱数 Unknown，不做猜测
3. 企业B/C 的部分账号原始密码未知，测试账号是隔离环境自建
4. 560/720 响应式断点未在本轮三端截图外做真机测试

## 七、试点建议

**具备小企业付费试点条件，限定：物流行业、内部授权试用、人工确认财务。**
进入下一阶段前需要老大决策的商业事项：
1. 试点定价与 token01 预算上限（本轮共消耗约 40 万 token，含优化前任务）
2. 是否继续用钥匙串 BART-TOKEN01-API 做后续深度验收
3. 第二个行业业务包的范围（如需跨行业销售）

## 八、代码提交

- `a55fce8` 工具审计/记忆契约/能力暴露修复
- `5b55863` 上下文 token 成本优化
- `30d8e55` 通用内核与物流能力解耦
- 全量测试 141 项全过；四家企业服务健康；共享前端三端无溢出

原始证据：`/tmp/julong-v5-visual/acceptance/evidence/`（对话响应、xlsx、三端截图、故障注入日志）。

---

## 九、最终收尾（GPT 复查三处 + 部署准备）

### GPT 复查发现的三处问题（全部修复复测）
1. **企业模块未完整约束服务端**：非物流企业 HTTP 接口层仍可调物流接口。修复：Handler 增加行业守卫，物流专属路由（finance/reconciliation/waybills/cases/routine-scans）在非物流行业返回 404。实测 5 个物流接口全 404，通用接口（today/state/notifications）200
2. **非物流暴露物流团队工具**：`get_case_workspace`/`handoff_case`/`share_case_file` 来自 team 模块被漏过滤。修复：按行业包一并过滤。实测非物流企业 20 个工具、零物流工具
3. **正式规则写入与契约注释不一致**：`forget` 走 save 分支需版本匹配、行为错位。修复：forget 独立处理（个人偏好本人可撤回、企业记忆授权岗位、留痕），契约注释改为与实际 note/save/forget 行为一致。实测四种模式：note+rule 拒 / save+rule+版本 成 / save+experience 拒 / forget 成

### 真账数据兼容（只读副本演练，原件未碰）
- 真账副本 32 表、rev 69、8 票运单、3 回单、3 用户；本分支 `init_db()` 直接打开，自动建 `product_installation` 并绑定 julong-test，表 32→34，数据完整
- 原账号 JLWL/jnlong818 登录成功，前端 200
- **发现并处置**：JLWLJL/JLWLWL 原密码与老板相同这一说法实测不成立（登录失败）。试点前必须用老板会话经 `POST /api/users/<user_id>/password` 重设（收 user_id 非 username）；副本演练返回 200 且会话撤销

### 完整前端联调（真账副本，三角色）
- 老板真登录，五个页面全加载真数据：工作台 16 条回单催办、持续任务 2 条真 job、业务中心 3 模块、详情面板可开、无 JS 错误；桌面 1440 布局正常；手机/平板见第二轮三端截图
- 三角色：老板/调度 finance=true+财务接口 200；员工 finance=false+财务接口 403+金额脱敏
- "工作记录"显示暂无——迁移前旧会话不在新 AI 会话表，符合预期

### 备份恢复演练
- `scripts/ledger_backup.backup()` 生成带 SHA256 的 manifest；`restore()` 校验每文件哈希 + SQLite 完整性 + manifest 与账本引用一致，恢复到新目录
- 实测：恢复到新目录后 rev 69 / 8 票 / 3 回单 / 3 用户全一致

### 真实模型计费（查网关公开 API，非猜测）
- 源：token01 `/api/pricing` 实测；deepseek-v4.1-flash 默认组 billing_expr 闲时(22:00–08:00) `p*1+c*4`、忙时 `p*2+c*8`（p/c=每百万 token）；`quota_display_type=CNY`、`quota_per_unit=500000`
- 本轮全部真实调用：25 次成功、859,346 token（prompt 832,000/completion 27,346），全在闲时
- **闲时总成本 ≈ ¥0.94；忙时 ≈ ¥1.88；单次平均闲时 ≈ ¥0.04**
- Key 实际分组未查到（折扣组会更低）；按默认组公式，口径透明不猜

### 最终结论
**具备生产试点条件**，限定：物流行业、内部授权试用、财务人工确认、授权后才切换环境。
未经授权不切生产、不覆盖真账。部署清单、启动顺序、恢复方案、禁止项见 `acceptance/DEPLOY-PREP.md`。
仍需老大决策的商业事项：试点定价与预算、是否继续用现有 Key、第二行业包范围（本轮不开发）。
