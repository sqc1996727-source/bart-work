# 交接文档 · 物流 AI 工作台

> 给接手的新会话看。读完这份 + `docs/界面设计规范.md` 就能干活。
> 最后更新：2026-11-24 17:36

## 一、这是什么

**一个面向小物流公司的 AI 工作台产品**（不是给某一家做的定制系统）。定位：不是聊天助手，是会主动干活的 Agent —— 自己扫账、找对账差异、催回单、涉钱要人确认。

**第一个试点客户：鞍山巨龙运输**（真实 8 票运单在跑）。用它跑通打磨，之后卖给更多小物流公司。所以**业务规则要能配置，不能写死**。

## 二、代码在哪

```
/Users/zhuzhu/WorkBuddy/2026-11-09-21-28-22/julong-trial/
```

技术栈：Python 标准库 `http.server` + `sqlite3`，**无第三方 web 框架**。业务数据存在 `work_state` 表的一行 JSON 里（10 个域），不是独立表。

关键文件：
- `app.py`（约 3000 行）—— 主服务、路由、工具链、权限、脱敏
- `finance_confirmation.py` —— **涉钱确认三步流程（核心，别乱动）**
- `capabilities/routine.py` —— 主动扫账（对账差异 + 回单催办）
- `capabilities/runtime.py` —— 后台 worker、定时任务、通知、发消息
- `capabilities/workplace_memory.py` —— 工作记忆（版本+岗位隔离+有效期）
- `business_integrity.py` —— 业务校验、冲销规则
- `frontend/v4/` —— **当前主力前端**（GPT 设计稿 + 真数据）
- `frontend/v3/` —— 上一版液态玻璃前端（保留，别删）
- `manual_v9/workbench.html` —— 老手动台，费用确认闭环完整

## 三、怎么跑起来

```bash
cd /Users/zhuzhu/WorkBuddy/2026-11-09-21-28-22/julong-trial
python3 app.py serve        # 监听 127.0.0.1:3000
```

- 新前端：`http://127.0.0.1:3000/agent-v4/`
- 老板账号：`JLWL` / `<TEST_PASSWORD>`
- 管理账号：`JLWLJL`　员工账号：`JLWLWL`（密码同老板，未验证）
- 老手动台：`http://127.0.0.1:3000/manual-v9`

**启动服务必须用后台方式**（`run_in_background: true`），普通 Bash 调用结束后进程会被回收。

跑测试：
```bash
python3 -m unittest discover -s . -p "test_*.py"     # 97 项，应全过
# 缺依赖先装：pip install rapidfuzz==3.14.6
```

## 四、后端状态：功能齐了，97 项测试全过

已实现：主动扫账、定时循环扫账（按上海时区）、涉钱三步人审+留痕、任务态回查、工作记忆、发消息。

**Codex 独立审查修完 13 个问题**（3 个 P0 + 7 个 P1 + 3 个 P2），我逐条验过代码：
- P0-1 费用确认不能自己签自己（`finance_confirmation.py` 有真密码重认证 + 证据指纹 + 作废流程）
- P0-2 员工扫账只扫自己运单的回单，无财务候选
- P0-3 厂家运费只走独立对账接口，普通运单走白名单 `_ORDINARY_WAYBILL_FIELDS`（不含 amount）

**但：没在真实账、真实模型上验收过。全部是合成数据 + 临时库。**

## 五、前端状态：v4 正在调

v4 是老大让 ChatGPT 设计、我接上真实后端的：
- `index.html` + `assets/window.css` = **视觉基准，来自 GPT 设计稿，不许自己改材质**
- `assets/adapter.js`（124 行）= API 封装
- `assets/app.js`（1390 行）= 业务逻辑，接的是真 API

**已经踩过的坑，别再踩：**
1. 我曾把布局改成"铺满全屏"，毁了原稿"暗色窗口浮在浅灰蓝桌面"的空间感 —— 老大直接说"一点不一样"。**现在是对的：窗口 1180 宽居中 + 四周露出渐变桌面 + 内容多了内部滚动**。
2. 我加过登录墙，老大第一眼看到的不是工作空间 —— 这是必要的（要认证），但要跟他说清楚。
3. `?auto=1` 自动登录后门截图用完必须删。

## 六、业务铁律（产品级，代码强制，不靠提示词）

1. **价格保密**：厂家运费（运单 `amount`）只在内部对账出现。运单详情、列表、搜索、导出、打印一律不露。员工完全看不到金额。
2. **涉钱必须人审**：费用确认走 `finance_confirmation` 三步（建候选 → 审阅 → 重输密码确认），全程留痕。Agent 只能出候选，不能替人签。
3. **角色三档**：`owner_finance`（全开）/ `dispatcher`（单票与线路成本，无公司整体利润）/ `employee`（只有自己的运单回单，无金额）。

## 七、已知问题（未解决）

| 问题 | 说明 |
|------|------|
| **调度看不到费用金额** | `dispatcher` 调 `/api/state` 时 `amount` 被脱敏剔除，导致"能批钱却看不见多少钱"。疑似过度脱敏，需老大定口径。**未修。** |
| `/api/agent/chat` 本机 500 | 本机没配 AI 模型（`JL_AI_API_KEY`），正常，上线才验 |
| v4 右栏内容挤 | "企业业务数据 运单8票、回单3份、费用0笔"挤成一坨，字太小 |
| 通知是 15 秒轮询 | 不是推送 |
| 「安排跟进」没专用接口 | 目前只是把话丢给 chat |
| 产品化欠账 | `tenant_id` 硬编码 `julong-test`、费用类型枚举写死 `{进仓费,送货费,等时费,其他}`、合作方名单是巨龙的 |

## 八、API 契约（v4 在用）

```
POST /api/login                      {username,password}
GET  /api/agent/ui/bootstrap         {user,suggestions,capabilities}
GET  /api/agent/today                {routine:{routine_candidates[],total,by_routine,note,coverage},pending_state[]}
GET  /api/state                      {rev,state:{julong_demo_fees_v1:"JSON字符串",...}}  ← 值是字符串要二次 parse
POST /api/finance/candidates         {password,kind:"fee",fee_id,status} → 201 {candidate_id}
GET  /api/finance/candidates/<id>    {fee,target_status,expected_rev,stale,completed}
POST /api/finance/candidates/<id>/confirm  {password}
GET  /api/finance/candidates         待审列表
POST /api/agent/chat                 {text,memory_scope}
GET  /api/agent/notifications
POST /api/agent/notifications/seen   {id}
GET/POST /api/agent/followups        定时任务
```

## 九、工作方式（老大定的）

- **重活派子 agent**：`subagent_type: general-purpose`，`model: Deepseek-V4.1-Flash-1017`。我只管拆活、写约束、验收。子 agent 看不到对话，**派活必须把业务铁律和文件边界写进 prompt**。
- **证据标准**：代码写过 ≠ 跑过；跑过 ≠ 业务成了；HTTP 200 ≠ 成功。汇报分四层：改了什么 → 部署没有 → 跑起来没有 → 业务结果如何。
- **评审权归老大**：前端好不好看，我不许自己下判断，只报客观事实（结构对不对、语法过不过、功能通不通）。
- **别自作聪明改设计**：视觉基准是 GPT 设计稿，改之前问。
- **只在本地做，别上线**。别碰 `data/`（真账）。
- **不确定标 UNKNOWN**，别猜着装懂。

## 十、下一步该干的（按优先级）

1. **修「调度看不到金额」** —— 但要先问老大：调度的费用确认场景看不看得到金额。涉及价格保密铁律，别自己定。
2. 收拾 v4 右栏挤的问题。
3. 用真账副本验一遍（费用类型映射、历史备注会不会误判）。
4. 前端 v3 → v4 切换决策。
