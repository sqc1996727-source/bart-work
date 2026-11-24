# 微定制企业 Bot · 试点版产品说明

正式候选分支：`release/stable-pilot-20261125`。独立验收以仓库根目录 `acceptance/STABLE-PILOT-REVIEW.md` 为准；部署与回滚以 `acceptance/DEPLOY-PREP.md` 为准。此文不是生产验收证明。

## 架构与使用
- 一套共享前端 `/workspace/`，HTML、CSS、JS 分离，保留设计基线及交互动效。
- 一套轻量 Agent 内核：持久任务、模型工具调用、任务调度、文件处理、企业和个人记忆。
- 企业配置由部署者管理：行业包、模块、角色。模块策略集中在 `module_scope.py`；物流查询实现集中于 `capabilities/logistics_read.py`，关闭模块不删除原始数据。
- 一家企业一个数据目录、进程、数据库和独立生产域名。新企业不得使用既有企业的数据目录；现有巨龙物流使用原 legacy 标识。
- 当前仅有物流行业业务包；其他行业可使用通用能力及上传文件，不宣称已有其行业业务数据接口。
- 正式模式仅使用持久任务接口。原同步聊天和旧 Agent 页面保留兼容/测试用途，不作为正式入口。
- 共享工作空间含财务候选审阅与密码确认、账号管理及个人/企业工作范围。现有物流人工录单、打印与对账功能复用 `/manual-v9`，通过业务中心进入，不为每家企业重新开发。

## 隔离启动
使用 Python 3.11+，在后端目录建立独立 venv，安装 `requirements.txt`（使用本地 OCR / 语音时另见 requirements-media.txt）。示例：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
export BOT_PROFILE="$PWD/profiles/logistics.example.json"
export DATA_DIR="$(mktemp -d)"
export COOKIE_SECURE=0 HOST=127.0.0.1 PORT=3101
.venv/bin/python app.py user-add --username demo-owner --name "测试负责人" --role owner_finance
# 已获授权模型配置由本机安全环境或钥匙串注入；不将密钥写入仓库。
export PYTHON_BIN="$PWD/.venv/bin/python"
/bin/sh scripts/run_pilot.sh
```

打开 http://127.0.0.1:3101/workspace/ 。正式使用前通过 `/health` 检查数据库、任务线程心跳与模型配置；健康检查不调用外部模型，不证明供应商可用。生产须使用 HTTPS、独立域名、Secure Cookie、现有服务管理器与受控备份目录。

## 验收
本分支在当前编辑环境只进行了部分精确源码测试和静态 JavaScript 检查；完整回归、真实模型、浏览器及迁移需在隔离环境执行，未通过前不得部署生产。旧接口兼容测试可设置 `BOT_STRICT_PILOT=0`，正式启动脚本强制为 1。

```sh
BOT_STRICT_PILOT=0 .venv/bin/python -m unittest discover -s . -p 'test_*.py'
```

不在仓库中保存真实凭据、客户记录、真实备份或业务文件。
