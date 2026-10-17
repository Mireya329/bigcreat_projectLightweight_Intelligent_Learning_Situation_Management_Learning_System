# 组长 AI 网关第三轮交付包 v2.1

2026-10-12。本包用于合入项目 vendor/captain_ai_v2，包含完整网关源文件。第三轮回复.md 为当前交付说明。

## 启动

在此目录运行 `python -m unittest discover -v`。
先启动 Ollama 并安装模型，然后设置环境变量并启动网关（PowerShell）：

```powershell
$env:CAPTAIN_MODEL = 'qwen2.5:7b-instruct-q4_K_M'
python server.py --port 8765
```

后端按原项目设置 AI_PROVIDER=gateway、AI_GATEWAY_URL=http://127.0.0.1:8765/ai。确认模型变量名称以 captain_ai.py 为准。

GET /health 只验证网关存活，不证明模型可用。未完成真实数据和前端验收，分类保持人工复核。school_recommend 继续禁用。

reports 包含本次单元测试、真实后端协议测试及受控 HTTP 链路结果；受控模型输出不能作为模型质量证据。原仓库基线 c077373a9a0659d1d52c21bc79217ead775436a5。
