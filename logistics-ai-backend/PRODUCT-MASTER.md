# 企业 AI Bot · 产品母版 v1

基线：物流稳定版 release/stable-pilot-20261125，原 32df1b0 已由 WorkBuddy 独立验收（161 项测试通过）；此后补齐费用作废入口，并在独立 product/master-v1 分支提炼母版。后续改动尚未完成完整服务验收。不要将旧版验收结果当成本版全量验收结果。

## 1. 产品边界与长期维护原则

目标是一套产品源码、一个共享工作空间、可替换模型、一套通用 Agent 执行和数据治理机制；行业差异用行业包解决，企业差异用独立配置、业务数据、正式规则和工作知识解决。禁止为了不同客户复制一套前端、另写一套 Agent、把客户密码/数据/名称写进通用源码。

| 层 | 职责 |
|---|---|
| 通用产品 | 登录、人员角色、统一前端、对话、任务、持久执行/取消/恢复、定时执行、文件、分析、交付、记忆、知识、审计、用量、备份与健康 |
| 行业包 | 实际业务数据接口、工具契约、行业规则、模块及操作入口；物流是第一个已有业务实现 |
| 企业实例 | 独立 tenant_id、企业名称、开通模块、账号、数据目录、正式规则、历史文件、模型凭据与成本归属 |

未经接入的业务数据不能声称已读取，未经实现的行业操作不能假装已完成。general 不是另一个行业包，也不是所有行业的 ERP；它提供真实通用工作能力。行业业务连接需实际开发或适配。保留物流的旧数据结构及成熟人工页面是兼容决定，不表示所有行业共享物流字段。新行业数据可使用其独立表或已有系统接口。

## 2. 母版与实例

- 共享源码：product/master-v1 分支。独立物流发布分支只做该试点的受控维护。
- 企业配置：profiles/general.example.json、profiles/logistics.example.json 等仅为示例；实际配置和数据必须放在源码目录之外。
- 未指定 BOT_PROFILE 时，仅得到不可初始化的 neutral placeholder，不默认采用巨龙身份。正式脚本拒绝 legacy 和未配置身份。
- 同一应用可按企业分别运行，企业的数据目录、进程、端口、正式域名、凭据及模型用量各自管理；不宣称实现共享数据库多租户。
- 客户没有修改行业注册、任意增加模块或任意增加工具的接口。部署方修改配置；员工权限仍由服务端执行。
- 新企业首先用 general + 空模块接入通用能力，若已有适用行业包，再由部署者选择行业包及模块。不得迁移其他客户的账号、规则和账本。

## 3. 新企业初始化

从 logistics-ai-backend 目录执行，使用 Python 3.11+ 和独立虚拟环境。以下是合成示例，不是实际客户部署：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
INSTANCE="$HOME/enterprise-bot-instances/example-company"
.venv/bin/python scripts/create_enterprise.py \
  --instance-dir "$INSTANCE" --tenant example-company \
  --name "示例企业" --industry general
export BOT_PROFILE="$INSTANCE/profile.json"
export DATA_DIR="$INSTANCE/data"
.venv/bin/python app.py user-add --username owner --name "企业负责人" --role owner_finance
export HOST=127.0.0.1 PORT=3101 COOKIE_SECURE=0
export PYTHON_BIN="$PWD/.venv/bin/python"
# 由运营者在运行时注入既有已授权的 JL_AI_BASE_URL/JL_AI_MODEL/JL_AI_API_KEY；
# Mac 可用已有授权的钥匙串项 BOT_MODEL_KEYCHAIN_SERVICE。
/bin/sh scripts/run_master.sh
```

仅隔离 HTTP 本地测试使用 COOKIE_SECURE=0。正式部署须 HTTPS、Secure Cookie、独立域名和独立端口、私有配置/日志目录及自动重启。检查 /health 的真实就绪状态；不把它当外部供应商的健康保证。

行业物流示例使用 --industry logistics --modules transport,receipt,finance；按实际客户选择。已有试点 legacy 迁移继续使用独立发布分支的部署文档，不将本母版直接指向真实账本。

## 4. 新行业能力接入约定

行业包注册由源码维护者完成，而不是让模型动态载入客户给出的代码。

- industry_packs/ 注册行业名、模块名称、角色、描述、AI 入口 prompt、可选的已实现界面操作。模块、工具、路由名称必须全局唯一；新行业使用自己的命名空间，不能复用 transport、receipt、finance 等已有物流内部模块标识。禁止覆盖共享工具。
- 工具归属统一声明在 tool_groups；adapter_tools 声明 schema、execute(host,user,args,key) 和可选 read_only=True。默认视为写工具，使用通用执行日志和防重复机制；适配器仍负责实际业务校验、事务、记录权限及幂等。不要把未核实写入结果列为可安全恢复。
- initialize(host,profile) 只做必要的幂等、增量 schema 初始化，不能自动覆盖业务数据。
- 独立 JSON HTTP 接口使用 /api/industry/<industry>/...，adapter_routes 注册 method、精确路径、模块、处理函数；route_groups 声明该模块前缀。共用登录认证与模块/角色检查。处理函数继续做具体业务记录授权和必要的人工确认。
- 前端依据 bootstrap 模块元数据渲染业务入口；已有操作类型为 link、finance_review、fee_void。link 必须是同源、实际实现的页面，不能把一个不存在的页面当作功能。专用交互组件仅在业务确实需要时增加并复用。通用工作台默认展示真实工作任务；没有行业数据不生成假巡检。
- 新行业可提供业务工具及数据连接，但不可宣称已经具备尚未实现的行业巡检/专用 UI。已有物流实现保留在原稳定业务代码中并受同一模块约束；没有为追求目录形式重新编写已经验证的业务。

## 5. 企业知识与微定制

不要靠企业特制提示词代替真实数据。新企业从空白实例接入自己的原始材料，经 Agent 提取候选口径、指出冲突和数据缺口；正式规则按已有版本机制、有效日期及授权保存。个人偏好、企业经验、正式规则、业务记录、原始文件、会话和任务状态分别保留。行业能力、企业规则和实际执行互不冒充。

## 6. 当前验收证据与复测

本次实际完成：
- 本地从远程源码重建七个文件并用 git hash-object 逐一确认与 GitHub blob 完全一致；Python 编译通过。test_product_master.py 本地与远程 blob 一致（0fba7f6759140ce2418878be4383889dd6a77ba9）。
- 5 项核心契约测试实际通过：未配置保护、两个空白实例/账本绑定、模块与角色投影、注册工具/接口的 SQLite 实际读取与关闭限制、真实任务的企业及员工范围。
- 两个分支共享前端 JavaScript 语法解析通过；使用合成 HTTP 响应执行真实作废表单函数，确认网络错误后复用同一幂等键并进入候选审阅，不直接确认费用。
- 尚未执行新增物流作废 Python 回归、母版全量 Python 回归、完整真实服务/浏览器/模型或外部部署验收。编辑环境无法解析 GitHub 主机名，不能克隆完整仓库。核心测试不是生产证明。

WorkBuddy 独立验收，仅测试不修改源码，使用隔离虚拟环境和合成数据：
1. 拉取稳定物流分支，运行 test_stable_pilot 与全量回归；验证 Agent/人工作废、原因必填、权限、候选、错误/正确密码、审计和原记录保留。
2. 拉取母版，先运行 python -m unittest -v test_product_master test_product_profile。历史物流兼容测试使用 **显式** BOT_LEGACY_COMPAT=1、BOT_STRICT_PILOT=0 和隔离 DATA_DIR 执行全量回归；不要将兼容模式作为母版正式启动方式。
3. 从 create_enterprise.py 创建两个不同企业：通用企业 + 物流企业；各自独立数据/端口和账号，通过 run_master.sh 正常启动。验证未配置时拒绝启动，不读取任何既有企业目录。
4. 同一共享界面：通用企业显示真实任务而非物流巡检；Agent 真执行文件/表格/任务/记忆/定时工作；无物流工具及路由，个人和企业范围正确。物流企业已有能力和财务作废仍正常。验证关闭模块及跨企业访问失败。
5. 记录实际测试结果及失败证据。未经授权不切换生产、不动真实账本、不新增付费。不得将未验证能力写成通过。

## 7. 发布纪律

代码母版和客户实例分离，客户实例只持有配置及数据。每次母版更新先隔离测试再发布，记录版本及回滚点；先使用现有备份恢复机制保护数据。此前公开历史中曾出现真实口令：本分支未改写远程历史、未轮换真实口令、未改变仓库可见性；必须在实际部署前完成凭据轮换。不要将包含历史提交及其他项目的整个仓库对外分发；对外分发前另行生成并审查干净源码快照，不带任何客户资料、配置、备份或凭据。

剩余真实生产操作仍需授权和实际环境验证：口令轮换、正式 HTTPS/反代、真实账本迁移与核对、需要的媒体能力、实际负载与稳定性。内部小规模试点不需要为尚未启用的行业功能或大规模并发扩张重构。
