# 稳定版发布候选 · 独立验收

基线：acceptance/module-scope-20261125。候选：release/stable-pilot-20261125。
此次整改只提交代码，不部署生产、不写真账、不变更凭据。历史 152 项测试不是本次结果。

## 改动范围

- 模块政策集中到 module_scope.py；混合业务读取、旧接口写入、任务及恢复操作受模块约束，禁用域保留原数据。
- 全快照替代不可靠时间增量；比较实际行内容、双向增删、关联文件。
- 工具事件持久记录所属任务；取消任务和严格模式中无法确认归属的旧事件不恢复。
- 严格模式默认开启，正式聊天走持久任务队列；旧同步入口停用，兼容测试可显式关闭严格模式。
- 共享界面接通财务候选审核及密码确认、人员管理、个人/企业记忆范围；保留人工物流工作台并适配模块。轮询与 SSE 避免并行重复监听。
- 历史文件工作原始结果按需检索，检查用户、记忆范围、文件权限；当前业务状态须重新查询。
- 明确记忆动作契约；健康检查、依赖清单、启动脚本和 launchd 示例。
- 物流查询拆出 capabilities/logistics_read.py；共享界面分离 HTML/CSS/JS；旧业务能力保留，不为减少行数删功能。

## WorkBuddy：只做验收，不修源码

从本分支新建隔离工作区，明确临时 DATA_DIR 和独立端口；依赖安装在隔离虚拟环境，沿用已有授权模型及预算。不得接触生产服务、真账、真实密码或未授权付费资源。

1. 检查当前提交和干净工作区；安装 requirements.txt；启用媒体能力时检查可选依赖。编译全部 Python，检查 JS。
2. 在隔离数据目录运行 test_stable_pilot.py 和全量 unittest discover（旧接口兼容用例显式 BOT_STRICT_PILOT=0）。报告实际通过/失败/跳过数量。
3. 严格模式启动真实服务：检查健康、模型配置、登录、三角色及模块开关；验证旧同步聊天接口拒绝而队列真实可用。
4. 含既有运单、回单、财务记录的合成账本：关闭模块后验证状态、混合查询、统计、附件、案卷、巡检及旧写入接口；修改启用域后重新开启模块，核对原禁用域无损、无越权。
5. 实际任务执行中取消，随后继续对话、重启恢复；核查事件归属、原任务不重放，正常任务仍可执行，既有写入不重复。
6. 真实浏览器验证桌面及手机：手工录单、审核候选、错误/正确密码、人员管理及最后管理员保护、个人/企业记忆切换、任务切换、附件及 SSE 断开重连。员工不可获取受限金额；界面不得伪造业务完成。
7. 测试同 ID 内容更新、增删行、文件缺失/篡改、快照恢复、旧应用兼容。所有操作均隔离。
8. 验证真实模型调用、按需找回历史文件工作结果、权限复核及正式规则/笔记/撤回契约。未执行或没有授权的真实测试标明未验证。
9. 检查启动脚本和服务管理示例、异常退出、健康状态；记录正式 HTTPS/反代和密码轮换是否仍未验证。

提交一份验收报告，列出真实运行命令、测试证据、失败复现与日志位置，结论按通过/失败/未验证分类。不要修改候选源码或进行生产切换。整改方负责处理发现的问题。

## 当前证据边界

GitHub 已提交源码。共享前端 JavaScript 静态解析检查通过。

本次独立执行的局部验证：从 GitHub 读取并在临时环境复制四个文件，用 git hash-object 确认字节与远程 Git blob 完全一致（module_scope.py 8cae139b3b2f37b464985f5c0af5a46f284a4cf7；product_profile.py 2239e15aa9b508ce137717467c2200abdeb00b01；scripts/pilot_cutover.py d44f914372bbfe4ebfe97e8e9063b35e4e5b6d48；scripts/ledger_backup.py dabb843bf9d386130cdef3c98c55b60bab7d6464）。四个文件编译通过。8 种模块组合的策略函数验证通过；真实临时 SQLite 两库和关联文件备份/恢复通过；同 ID 内容变化、文件篡改和缺失均被发现。测试使用合成数据。

当前编辑环境 GitHub DNS 不通，完整仓库无法克隆，尚未执行全量 Python/HTTP/浏览器/真实模型验收、完整依赖安装。生产运行、凭据轮换、反代和真实数据迁移仍待独立实测。不得把局部测试或源码提交当作已通过生产验收。


## 独立验收后的定向修复（基线 eaa8c8a）

WorkBuddy 报告该基线 157 项测试通过，9 组运行验收通过，但已确认费用非法状态变更被模型重复请求时出现 502；登录错误提示也不准确。该报告属于前一提交的实际验收，并非本次修复后的全量验收。

本次修改：
- finance_confirmation.candidate_rejection 对允许公开的业务校验码返回明确解释；V3Tools 不再将全部费用候选校验错误压成 invalid_request。不改变候选、财务审核、确认、作废等业务规则。
- AIService 仅对持久化的结构化拒绝结果（有 result_json 且 error 与持久错误码吻合）返回缓存结果；不再次执行。异常、取消、结果不确定仍失败关闭。fail_tool 清除结果字段以区别确定的业务拒绝。
- 登录区分 invalid_credentials、login_rate_limited 与会话认证失效。
- test_stable_pilot.py 新增四项针对性 Python 回归测试：真实合成账本中的拒绝与重复请求、合法候选去重、未知执行故障禁止恢复、异常信息脱敏。

当前环境直接执行远程 JS 的 api/fail 函数，使用合成 HTTP 响应验证四种情况通过：错误密码 401、认证失效 401、频率限制 429、其他权限拒绝 403。JS 语法检查通过。完整仓库克隆仍受 DNS 限制，本次新增 Python 测试及真实模型复现未运行。

定向复验：以当前最新提交建立隔离环境，不使用真实账本。运行 `BOT_STRICT_PILOT=0 python3 -m unittest -v test_stable_pilot`，再运行 `BOT_STRICT_PILOT=0 python3 -m unittest discover -s . -p 'test_*.py'`。严格模式使用已授权真实模型重跑 F-2001 场景：返回正常业务拒绝答复，不生成候选、不改账本、原样重复不执行、不产生 502；合法费用候选审阅确认仍正常，取消与异常恢复约束仍有效。浏览器实测错误密码与会话失效文案。报告实际结果，不自行修改源码、不切生产；原未验证的生产 HTTPS、密码轮换、真实账本迁移等事项继续保留为上线条件。


## Final business closure: fee reversal

Baseline 32df1b0 was independently reported passing 161 tests and targeted real-model/browser checks. The subsequent patch exposes the existing reversal status plus reason to the Agent and adds a manual form in the financial module. Both create only a candidate; the existing password review/confirm and audit path remains mandatory. One focused regression was added. New Python and browser runtime tests are pending. Verify missing reason, forbidden employee, finance-disabled access, correct candidate/reason, wrong password, correct confirmation, retained original amount/status=作废, and repeated confirmation without a duplicate write. Verify Agent does not choose reversal on its own after a rejected status change. No production changes.
