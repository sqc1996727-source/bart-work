# 巨龙物流真实业务试点 · 部署准备（未执行，需授权后 manual 切换）

> 本文件是准备方案，所有步骤已在隔离副本演练通过。**未经授权不得切换生产环境、不得覆盖真账。**

## 1. 部署拓扑（沿用现有，不新增服务）

- 单进程：`python3 app.py serve`，默认监听 `127.0.0.1:3000`
- 前端：内置 `/workspace/`（共享外壳，无需单独部署）
- 数据：每企业一个 `DATA_DIR`，内含 `julong.sqlite3` + `ai/ai.sqlite3` + 文件目录
- 入驻客户不设 `BOT_PROFILE` → 走 legacy `julong-test`，兼容现有真账

## 2. 环境变量清单（实测存在）

| 变量 | 作用 | 试点建议 |
|---|---|---|
| `DATA_DIR` | 企业数据目录（绝对路径） | 指向真账目录的**备份副本**，绝不指向原件做验证 |
| `HOST` / `PORT` | 监听地址端口 | 内网试点 `127.0.0.1` 或内网 IP；生产须 HTTPS 反代 |
| `COOKIE_SECURE` | 会话 Cookie Secure 属性 | 生产=1（默认）；仅本机 http 调试=0 |
| `BOT_PROFILE` | 企业档案 JSON | 巨龙 legacy 不设；新企业才设 |
| `JL_AI_BASE_URL` / `JL_AI_MODEL` / `JL_AI_API_KEY` | 真模型 | 沿用已授权 token01；Key 进程内注入，不落盘 |
| `JL_AI_TIMEOUT_SECONDS` | 模型超时 | 默认 30s |
| `BOT_AGENT_ROUNDS/MODEL_CALLS/SECONDS/TOKENS/RETRIES` | Agent 执行边界 | 默认 48/80/1200/250000/2 |

## 3. 部署前检查清单（逐项实测）

- [x] 真账副本能被本分支直接打开：`init_db()` 建 `product_installation` 表并绑定 `julong-test`，原 3 用户、rev 69、8 票运单、3 份回单完整（表 32→34，无数据丢失）
- [x] 原账号 `JLWL/jnlong818` 登录成功，前端 `/workspace/` 200
- [x] **调度 `JLWLJL` / 员工 `JLWLWL` 用同密码登录失败**（原 HANDOVER 写"密码同老板"未验证 → 实测不成立）。试点前必须用老板会话经 `POST /api/users/<user_id>/password` 重设（注意接口收 user_id 不是 username）。本轮已在副本演练，返回 200 且 sessions_revoked=true
- [x] 三角色权限：老板/调度有 finance 模块、财务接口 200；员工无 finance、财务接口 403、金额脱敏
- [x] 全量测试 141 项通过
- [ ] 生产环境 HTTPS / Cookie Secure / 反代配置：需运维配置，本 isolating 环境未验

## 4. 启动步骤（切换当天的顺序，需授权）

1. 停旧服务；对真账目录做一次 `scripts/ledger_backup.py backup()` 备份（带 SHA256 manifest）
2. 确认备份 manifest 校验通过；把备份复制到独立恢复目录做一次 `restore()` 演练并启动验证
3. 用真账**副本**先跑 24h 观察（不直接切原件）
4. 观察通过后，才在授权窗口内把 DATA_DIR 指向真账目录启动；启动即自动 `init_db()` 迁移（幂等，不覆盖数据）
5. 重设调度/员工密码，验证三角色登录
6. `/health` 200 + `/workspace/` 200 + 老板登录 + 今日工作有候选 → 判定启动成功

## 5. 恢复方案（已演练）

- 备份：`scripts/ledger_backup.backup(data_dir, backup_dir)` → 生成带 SHA256 的 manifest + `julong.sqlite3`/`ai/ai.sqlite3`/文件目录快照
- 恢复：`scripts/ledger_backup.restore(backup_dir, new_dir)` → 校验每个文件 SHA256、检查 SQLite 完整性、核对 manifest 与账本文件引用一致后恢复到**新目录**（不覆盖原目录）
- 实测：备份→恢复到新目录后，rev 69 / 8 票 / 3 回单 / 3 用户全部一致
- 回滚：停新服务，把 DATA_DIR 指回原数据目录/旧版本即可；备份目录保留不删
- 注意：这是"恢复到新目录"的安全设计，不要手工把备份拷回原目录覆盖

## 6. 真实成本（按网关公开计费公式，非估算）

- 数据源：token01 `/api/pricing` 实测，`deepseek-v4.1-flash` 默认组 `billing_expr`：闲时（北京时间 22:00–08:00）`p*1+c*4`，忙时 `p*2+c*8`（p/c = 百万 token）；`quota_display_type=CNY`, `quota_per_unit=500000`
- 本轮全部真实验收：25 次成功调用、859,346 token（prompt 832,000 / completion 27,346），全部跑在闲时
- **闲时成本 ≈ ¥0.94，忙时 ≈ ¥1.88，单次平均闲时 ≈ ¥0.04**
- 分组说明：Key 实际所属分组未查到（若走折扣组成本更低）；以上按默认组公式，口径已在报告中标注，不猜测
- 月成本示例（按真实单价外推）：若每天 100 次中等任务（约 4.3 万 token/次、闲时），月约 ¥120–260；实际以分组与用量为准

## 7. 试点判定与禁止项

- 具备"内部授权 + 人工确认财务"的物流试点条件（见 REPORT.md）
- **禁止**：未经授权切换生产环境；直接改真账；关闭财务三步人审；用模拟模型冒充真模型验收
