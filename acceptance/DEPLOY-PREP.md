# 巨龙物流真实业务试点 · 部署准备与回滚方案（未执行，需授权后手动切换）

> 本文件是准备方案，所有步骤已在隔离副本演练。**未经授权不得切换生产环境、不得覆盖真账。**
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
| `COOKIE_SECURE` | 会话 Cookie Secure 属性 | 生产=1（默认）；仅本机 http 调试=0 |
| `BOT_PROFILE` | 企业档案 JSON（含 modules 开关） | 巨龙 legacy 不设；新企业才设 |
| `JL_AI_BASE_URL` / `JL_AI_MODEL` / `JL_AI_API_KEY` | 真模型 | 沿用已授权 token01；Key 进程内注入，不落盘 |
| `JL_AI_TIMEOUT_SECONDS` | 模型超时 | 默认 30s |
| `BOT_AGENT_ROUNDS/MODEL_CALLS/SECONDS/TOKENS/RETRIES` | Agent 执行边界 | 默认 48/80/1200/250000/2 |

## 3. 部署前检查清单（逐项实测）

- [x] 真账副本能被本分支直接打开：`init_db()` 建 `product_installation` 表并绑定 `julong-test`，原 3 用户、rev 69、8 票运单、3 份回单完整（表 32→34，无数据丢失）
- [x] 原账号 `JLWL` 登录成功（口令不写进文档），前端 `/workspace/` 200
- [x] 调度 `JLWLJL` / 员工 `JLWLWL` 用老板同口令登录失败（原 HANDOVER 写"口令同老板"未验证 → 实测不成立）。试点前必须用老板会话经 `POST /api/users/<user_id>/password` 重设（接口收 user_id 不是 username），本轮已在副本演练 200 且 sessions_revoked=true
- [x] 三角色权限：老板/调度有 finance 模块、财务接口 200；员工无 finance、财务接口 403、金额脱敏
- [x] 模块关闭收口：同一行业内关掉 finance 模块时，`update_fee_status`/`review_business_records` 不再出现在模型工具表、执行返回 `forbidden`、`/api/finance/*` 与 `/api/reconciliation/*` 返回 404、状态接口费用域为空、扫账 coverage 标注 `finance_module_disabled`；关掉 transport 模块时运单接口与案卷/团队物流工具一并关闭（新增 11 项回归测试）
- [x] 记忆写入契约：`save_workplace_memory` 缺 `action` 一律拒绝（`explicit_action_required`），不再按 kind 推断成 save；正式规则必须 `action=save` + `expected_version`
- [x] 全量单元测试 152 项通过
- [ ] 生产环境 HTTPS / Cookie Secure / 反代配置：需运维配置，本隔离环境未验

## 4. 切换窗口操作顺序（需授权）

记 `t0` = 允许写入新版本的时刻（建议选低峰，例如 21:30）。

1. **冻结写入**：公告停用旧入口，确认无进行中的导入/审批（`/api/agent/jobs` 无排队）。
2. **全量备份**：`scripts/ledger_backup.backup(data_dir, backup_dir)` 生成带 SHA256 manifest 的快照，校验通过。
3. **恢复演练**：把该备份 `restore()` 到独立目录并启动试跑，确认 rev / 运单 / 回单 / 用户数一致。
4. **记录切换基线**：`scripts/pilot_cutover.py export --data-dir <真账> --since <t0> --out <delta>`，先出一份空基线（此时应为 0 行），确认工具能在真账上只读运行。
5. **切换**：停旧服务 → `DATA_DIR` 指真账 → 新版本启动（启动即幂等 `init_db()`，只加表不改数据）。
6. **验收**：`/health` 200、`/workspace/` 200、老板登录、`/api/agent/today` 有候选、三角色权限正确。
7. **重设调度/员工口令**，确认会话撤销生效。
8. **保留**：旧版本代码、切换前备份、`t0` 值三者都写在交接单上——回滚全靠这三样。

## 5. 回滚方案（三级，按破坏性从小到大）

**原则：回滚优先只回应用不回数据。试点期间新增的业务数据一律先落增量包，再谈恢复。**

### L1 应用级回滚（默认，不丢数据）

- 停新服务 → 用旧版本代码、`DATA_DIR` 仍指原真账目录启动。
- 新版本只做了**加表**（`product_installation`、`routine_scans`、`workplace_memory` 等）与幂等列追加，旧版本读写 `work_state` 不受影响；多余表旧版本会忽略。
- 启动后核对：rev、运单数、回单数、用户数与切换前一致；抽查 3 票运单详情。
- 适用：新版本出现功能性故障、性能问题、权限异常。**这是唯一不需要动数据的回滚。**

### L2 数据级回滚（仅当数据被写坏）

1. 先 `pilot_cutover.py export --since <t0>` 导出试点期全部新增/变更行（几百行级别，秒级完成）。
2. `pilot_cutover.py verify` 核对增量包与源库一致（哈希 + 行数）。
3. `ledger_backup.restore(备份目录, 新目录)` 恢复到**新目录**，不覆盖原目录。
4. `pilot_cutover.py compare --delta <增量包> --data-dir <恢复目录>`：列出"回滚会丢的行"。
5. 按 compare 结果逐表人工确认合并（只并试点新增，不覆盖历史字段），合并后再 `verify` 一次。
6. `DATA_DIR` 指向合并后的目录启动，核对业务数。

### L3 灾难恢复（磁盘/库损坏）

- 用最近一次 `ledger_backup` 快照 + 最近一次增量包重建；恢复后按 L2 第 4-6 步合并试点数据。

### 回滚禁止项

- 禁止把备份**直接拷回原目录**覆盖（会丢试点期数据且无法追）。
- 禁止在未导出增量包的情况下 restore。
- 禁止用 `rm`/覆盖方式处理真账目录。

## 6. 试点期数据保全（每次回滚前必跑）

```bash
# 1) 导出 t0 之后新增/变更的行（只读源库）
python3 scripts/pilot_cutover.py export --data-dir <真账> --since <t0> --out /path/to/delta
# 2) 核对增量包没坏、和源库一致（篡改或漏行会非零退出）
python3 scripts/pilot_cutover.py verify --data-dir <真账> --delta /path/to/delta
# 3) 回滚前比对：恢复出来的旧库缺哪些试点数据
python3 scripts/pilot_cutover.py compare --delta /path/to/delta --data-dir <恢复目录>
```

- 覆盖库：`julong.sqlite3` 与 `ai/ai.sqlite3` 都会扫；按 `created_at/updated_at/effective_at/at` 判增量，行身份用 `id`/`key`/`rowid`。
- 已演练：导出 441 行 → verify 通过；删 1 行模拟丢失 → compare 准确报出 `routine_scans: 1 行`；篡改增量文件 → verify 非零退出。
- 建议节奏：切换当天、每天收工、每次发布前各出一次增量包，保留 7 天。

## 7. 真实成本（以网关日志实际扣费为准）

- 计费源：token01 `/api/pricing`（`deepseek-v4.1-flash` 闲时 `p*1+c*4+cr*0.1`，忙时 `p*2+c*8+cr*0.1`；`quota_per_unit=500000`，即 500,000 quota = ¥1）
- **实测扣费**（`/api/log/token` 该 Key 日志，2026-11-25 00:15–01:20）：129 次调用、prompt 1,314,374 + completion 58,010 token，实扣 235,412 quota ≈ **¥0.47**；命中折扣组 3 折 / 3.3 折 / 3.6 折（另有 2 次 glm-5.3-flash 4.9 折）
- **折算单价**：约 ¥0.34 / 百万 token（折扣组实扣），比按标价估的 ¥0.94 低约 2/3
- **该 Key 累计用量**：`/v1/dashboard/billing/usage` 返回 498.82（分）≈ ¥4.99，含该 Key 全部历史与其他用途（token 名为"视频"，疑共用），**不能全部记到本项目**
- 口径说明：本项目历史调用未按项目打标，精确归属 UNKNOWN；上表按时间窗 + 模型过滤，可复核
- 月成本示例（按实扣单价外推，每天 100 次中等任务约 4.3 万 token）：约 ¥45–90/月；实际以分组与用量为准

## 8. 试点判定与禁止项

- 具备"内部授权 + 人工确认财务"的物流试点条件（见 REPORT.md）
- **禁止**：未经授权切换生产环境；直接改真账；关闭财务三步人审；用模拟模型冒充真模型验收
