# 巨龙物流真实业务试点 · 部署准备与回滚方案（未执行，需授权后手动切换）

> 本文件是准备方案。历史隔离演练和 152 项测试属于整改前验收分支；本次发布候选须重新验收。**未经授权不得切换生产环境、不得覆盖真账。**
> 文档不写任何登录口令、API Key 或凭据；凭据只走运行期注入与本机钥匙串。

## 1. 部署拓扑（沿用现有，不新增服务）

- 单进程：`python3 app.py serve`，默认监听 `127.0.0.1:3000`
- 前端：内置 `/workspace/`（共享外壳，无需单独部署）
- 数据：每企业一个 `DATA_DIR`，内含 `julong.sqlite3` + `ai/ai.sqlite3` + 文件目录
- 试点企业不设 `BOT_PROFILE` → 走 legacy `julong-test`，兼容现有真账
- 模块开关：`BOT_PROFILE` 的 `modules` 一旦配置，服务端工具、HTTP 接口、状态接口、扫账范围同时按模块收口（本次修复项）

## 2. 环境变量清单（实测存在）

| 变量 | 作用 | 试点建议 |
|---|---|---|
| `DATA_DIR` | 企业数据目录（绝对路径） | 指向真账目录的**备份副本**做验证，切换时才指真账 |
| `HOST` / `PORT` | 监听地址端口 | 内网试点 `127.0.0.1` 或内网 IP；生产须 HTTPS 反代 |
| `BOT_STRICT_PILOT` | 正式入口与兼容入口切换 | 正式=1（serve 默认）；0 仅用于隔离的旧接口兼容测试 |
| `COOKIE_SECURE` | 会话 Cookie Secure 属性 | 生产=1（默认）；仅本机 http 调试=0 |
| `BOT_PROFILE` | 企业档案 JSON（含 modules 开关） | 巨龙 legacy 不设；新企业才设 |
| `JL_AI_BASE_URL` / `JL_AI_MODEL` / `JL_AI_API_KEY` | 真模型 | 沿用已授权 token01；Key 进程内注入，不落盘 |
| `JL_AI_TIMEOUT_SECONDS` | 模型超时 | 默认 30s |
| `BOT_AGENT_ROUNDS/MODEL_CALLS/SECONDS/TOKENS/RETRIES` | Agent 执行边界 | 默认 48/80/1200/250000/2 |

## 3. 历史验收记录（整改前分支；非本次验收结论）

- [x] 真账副本能被本分支直接打开：`init_db()` 建 `product_installation` 表并绑定 `julong-test`，原 3 用户、rev 69、8 票运单、3 份回单完整（表 32→34，无数据丢失）
- [x] 原账号 `JLWL` 登录成功（口令不写进文档），前端 `/workspace/` 200
- [x] 调度 `JLWLJL` / 员工 `JLWLWL` 用老板同口令登录失败（原 HANDOVER 写"口令同老板"未验证 → 实测不成立）。试点前必须用老板会话经 `POST /api/users/<user_id>/password` 重设（接口收 user_id 不是 username），本轮已在副本演练 200 且 sessions_revoked=true
- [x] 三角色权限：老板/调度有 finance 模块、财务接口 200；员工无 finance、财务接口 403、金额脱敏
- [x] 模块关闭收口：同一行业内关掉 finance 模块时，`update_fee_status`/`review_business_records` 不再出现在模型工具表、执行返回 `forbidden`、`/api/finance/*` 与 `/api/reconciliation/*` 返回 404、状态接口费用域为空、扫账 coverage 标注 `finance_module_disabled`；关掉 transport 模块时运单接口与案卷/团队物流工具一并关闭（新增 11 项回归测试）
- [x] 记忆写入契约：`save_workplace_memory` 缺 `action` 一律拒绝（`explicit_action_required`），不再按 kind 推断成 save；正式规则必须 `action=save` + `expected_version`
- [x] 全量单元测试 152 项通过
- [ ] 生产环境 HTTPS / Cookie Secure / 反代配置：需运维配置，本隔离环境未验

## 4. 正式切换（需另行授权）

- 先完成 STABLE-PILOT-REVIEW.md 的隔离验收；更换曾公开暴露的真实密码、撤销旧会话，核实正式 HTTPS 和代理配置。
- 停止旧服务、冻结写入及任务执行；使用 ledger_backup 保存并校验完整切换前快照，恢复到独立目录演练。
- 保存旧版代码、运行参数和快照路径。仅获授权后切换到 release 分支，使用严格模式启动；/health 必须真实就绪。
- 正式运行仅通过 /workspace/ 和持久任务队列。旧版页面保留兼容用途；必要人工录单沿用旧业务页面。凭据只在运行期注入。

## 5. 回滚

优先应用级回滚，但必须先验证旧代码能读取新数据库结构；不能仅凭表数或记录 ID 宣称兼容。任何回滚前停止服务并保存完整当前快照。
如确需数据级恢复，先恢复到独立目录，比较完整数据及关联文件；出现差异时不得覆盖原库，也不得直接宣称无损。合并方案须经业务核对及授权。

## 6. 完整数据保全（取代原来的按时间增量方案）

在 logistics-ai-backend 下执行，停服后操作，输出目录必须尚不存在：

```bash
python3 scripts/pilot_cutover.py export --data-dir "$ISOLATED_DATA" --out "$SNAPSHOT"
python3 scripts/pilot_cutover.py verify --data-dir "$ISOLATED_DATA" --delta "$SNAPSHOT"
python3 scripts/pilot_cutover.py compare --data-dir "$RESTORED_DATA" --delta "$SNAPSHOT"
```

旧 --since 参数仅为兼容保留；实际一律保全完整快照，不根据时间字段推测增量。compare 比较双向全表内容和受引用文件，非零退出代表存在差异或错误，必须核查。所有备份均含敏感企业数据，仅存受控目录，不上传仓库。保留旧备份及当前快照，不自动覆盖、删除或合并。

## 7. 真实成本（以网关日志实际扣费为准）

- 计费源：token01 `/api/pricing`（`deepseek-v4.1-flash` 闲时 `p*1+c*4+cr*0.1`，忙时 `p*2+c*8+cr*0.1`；`quota_per_unit=500000`，即 500,000 quota = ¥1）
- **实测扣费**（`/api/log/token` 该 Key 日志，2026-11-25 00:15–01:20）：129 次调用、prompt 1,314,374 + completion 58,010 token，实扣 235,412 quota ≈ **¥0.47**；命中折扣组 3 折 / 3.3 折 / 3.6 折（另有 2 次 glm-5.3-flash 4.9 折）
- **折算单价**：约 ¥0.34 / 百万 token（折扣组实扣），比按标价估的 ¥0.94 低约 2/3
- **该 Key 累计用量**：`/v1/dashboard/billing/usage` 返回 498.82（分）≈ ¥4.99，含该 Key 全部历史与其他用途（token 名为"视频"，疑共用），**不能全部记到本项目**
- 口径说明：本项目历史调用未按项目打标，精确归属 UNKNOWN；上表按时间窗 + 模型过滤，可复核
- 月成本示例（按实扣单价外推，每天 100 次中等任务约 4.3 万 token）：约 ¥45–90/月；实际以分组与用量为准

## 8. 试点判定与禁止项

- 历史验收见 REPORT.md；当前发布候选须完成重新验收，尚未批准上线。
- **禁止**：未经授权切换生产环境；直接改真账；关闭财务三步人审；用模拟模型冒充真模型验收
