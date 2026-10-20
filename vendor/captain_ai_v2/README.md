# 组长第四轮网关交付包 v2.2
本包供二号合入项目 vendor/captain_ai_v2，保留当前其他修改。后端待修点见第四轮回复.md，本包未修改业务 API。
## 运行
Python 3.9 及以上。网关只用标准库。先运行 python -m unittest discover -v（55 项），再启动 Ollama 并安装 qwen2.5:7b-instruct-q4_K_M，运行 python server.py --port 8765。
可用 CAPTAIN_MODEL 环境变量指定模型。后端设置 AI_PROVIDER=gateway 和 AI_GATEWAY_URL=http://127.0.0.1:8765/ai。localhost 指当前机器，不能替代其他电脑地址。
GET /health 只验证网关存活。版本应为 2026-10-19-captain-gateway-v2.2。运行时拒绝自由文本字段等于示例占位符；一次重试仍失败时 code=1、data=null，raw 仅用于排障。
## 证据
reports 包含本版网关回归、当前真实仓库后端协议测试、id15 历史原文回放和真实后端到 HTTP 网关的受控失败结果。没有进行新的真实模型质量测评或业务数据库操作。基础仓库提交 51865007add8ae952c7947a72ea16d0f926c32a4。
## 交接
用 SHA256SUMS.txt 核对完整性。本包仅为网关模块交付，不等于后端状态修复、前端完成或全项目通过验收。新包替换后重启网关和后端。
