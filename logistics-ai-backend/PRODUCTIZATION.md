# 微定制企业 Bot · 产品化第一阶段

本分支保留原来的服务端业务规则、人工财务确认、持久任务、文件处理与长期记忆。
新增一套共用前端 `/workspace/`，基于已验收 07 设计基线及 08 排版原型的 HTML、CSS、转场和触控实现；企业差异由服务端 bootstrap 提供，不按企业复制页面。

## 隔离试跑（不要使用原生产数据目录）
在物流后端目录、具有现有依赖的开发机上执行：
```sh
export BOT_PROFILE="$PWD/profiles/logistics.example.json"
export DATA_DIR="$(mktemp -d)"
export COOKIE_SECURE=0 HOST=127.0.0.1 PORT=3101
python3 app.py user-add --username demo-owner --name "测试负责人" --role owner_finance
python3 app.py serve
```
打开 http://127.0.0.1:3101/workspace/ ，使用刚创建的账号。
另一企业使用 `profiles/logistics-receipts.example.json` 和**另一个**独立 DATA_DIR、独立进程、端口；生产需独立域名、HTTPS 及 Cookie 安全属性。
企业 ID 与数据库绑定，拒绝串用。未设置 BOT_PROFILE 时保留原试点配置；严禁直接对真账启动本分支。示例配置与仓库中不得存密码、密钥和客户数据。

## 接口与验收
- 同一前端通过 /api/agent/ui/bootstrap 获取企业名称、角色及业务入口，客户界面无模块增减按钮。
- 使用真实 jobs/SSE（轮询兜底）、history、today、notifications、files、followups；任务状态由服务端返回，不模拟进度。停止任务不等于撤销已经完成的业务操作。
- 财务正式写入仍使用原有授权确认流程；老手动台 /manual-v9 与原 V3/V4 未替换。共享外壳尚未移植完整手动录入、专用财务审阅等界面。
- 模块清单当前只定义 UI 入口，并不关闭服务端业务权限或工具；所有权限由原服务端负责。完整行业能力注册、企业级工具配置、通用业务数据结构仍需后续改造。
- 当前仅物流包；拒绝未实现的其他行业，不能宣称已经支持跨行业或共享数据库多租户。

```sh
python3 -m unittest -v test_product_profile
python3 -m unittest discover -s . -p 'test_*.py'
```
第一组测试已针对与 GitHub blob 一致的源码本地执行通过（6 项）；共享前端合并 JavaScript 已通过语法解析。完整回归、实际 HTTP/浏览器联调、真实模型和业务验收未执行。必须先在隔离环境完成这些验证，再考虑合并或部署。
