# 组长AI交付包第二轮修订版

发布日期：2026年10月9日。此包针对2号成员《回复队长 第二轮反馈》修订，替代9月27日旧网关包。请将整个压缩包交给2号成员，不能只转发Word回复。

## 先启动再接入

需要Python 3.9以上、Ollama及已安装的qwen2.5:7b-instruct-q4_K_M。程序只使用Python标准库。首次下载模型需要联网，本模块推理固定调用本机Ollama。

在本文件所在文件夹打开PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File .\start.ps1 test
powershell -ExecutionPolicy Bypass -File .\start.ps1 gateway
```

保持网关窗口运行。另开窗口，切换到二号成员项目根目录后设置：

```powershell
$env:AI_PROVIDER = 'gateway'
$env:AI_GATEWAY_URL = 'http://127.0.0.1:8765/ai'
$env:AI_GATEWAY_TIMEOUT = '370'
# 完成下述字段适配后，再运行原项目的测试库重跑脚本
python src/backend/ai_recompare_7b.py --run
```

最后一行是2号反馈中的原项目命令，本包未包含或实际执行该脚本。不要在本包目录中运行它，也不要加--clear。后端与网关必须在同一电脑；127.0.0.1不是组长电脑的远程地址。

启动脚本会跳过Python 3.5等不兼容版本，可通过CAPTAIN_PYTHON指定Python完整路径。脚本执行策略仅对该次进程生效。若已有8765端口服务，请先关闭旧网关，避免连错版本；本包不会自动结束其他进程。

## 新版已经修正

- 所有响应包含protocol，明确版本、schema版本、code=2含义与重试归属。
- 文字input继续兼容，新增对象input，支持question/stem/text/essay及student_answer、reference_answer。政治作答既支持input.student_answer也支持user_context.student_answer，两处冲突会报请求错误。
- 计划数字越界夹到边界；缺失或null使用默认值；无效字符串、布尔值、对象和小数拒绝。实际生效值记录于raw.effective_plan。
- 学情使用真实计数生成可追溯诊断。correct_count=null保持未知，只有错题标签数时不推算正确率；此路径不调用模型，model明确标记为deterministic-statistics-v2。
- 没有明确学生作答时，归因置信度强制为0，原模型置信度留在raw；全部AI归因仍走人工复核。
- 保留择校禁用，judge_fragment继续留在2号的独立处理路径。

## 字段和协议入口

协议版本：2026-10-09-captain-gateway-v2。数据规范版本：2026-10-09-captain-schema-v2。

- GET /health：网关版本与存活状态，不代表模型已准备好。
- GET /schema：完整protocol与各任务data规范。
- POST /ai：四字段请求scene、subject、input、user_context。
- schemas.json：各任务data字段的权威清单。
- protocol.json：版本及重试语义。
- examples.json：六项功能与四个兼容/失败场景，共10个请求。
- backend_contract.py：后端响应校验、一次HTTP调用、人工复核分流示例。

主场景仍是error_analysis、qa、weak_diagnosis、school_recommend、essay_review。qa通过task=math_guidance、politics_quiz、study_plan分派。科目支持math、politics、english、cet4、cet6，并映射反馈中使用的POSTGRAD_MATH、POSTGRAD_POLITICS、POSTGRAD_ENGLISH、CET4、CET6。

## 必须同步修改二号的校验与渲染

本包没有拿到2号项目源码，因此不宣称零改动接入。按docs/01_第二轮回复与字段变更.md修改SCENE_SPECS和render_content，保留模型响应中的protocol，不能在后端重新贴旧版本。原有local分支不受本包影响。

网关仅负责模型输出格式错误的一次修复重试。请求错误code=2立即返回，模型错误/连续无效输出code=1；HTTP异常另行处理。修改请求后可以重新提交，这是新的请求，不是对旧错误请求自动重试。

网关单次模型超时180秒，后端建议370秒。繁忙返回HTTP 503，不无限重试。raw始终是对象；后端只能把经校验的data交给业务流程，不能直接存模型原文。

error_type仍是协议要求的四选一候选值；raw.review_required=true、低置信度或默认人工复核开关优先，不能直接把候选当正式分类展示或统计。没有作答时尤其不得显示“已确认方法缺失”等结论。

## 复现本包测试

```powershell
# 无模型回归
powershell -ExecutionPolicy Bypass -File .\start.ps1 test
# 通过临时HTTP网关进行真实模型与规则路径测试，结束自动关闭临时网关
powershell -ExecutionPolicy Bypass -File .\start.ps1 live
# 已启动固定端口网关时，也可使用Python 3.9以上运行
python verify_delivery.py --url http://127.0.0.1:8765/ai
```

真实测试结果写入reports，不访问任何正式数据库。当前10个案例中7个实际调用Ollama，1个使用确定性学情规则，2个验证失败分支，不把10个案例全部称为模型调用。详情见docs/02_验证结果与项目剩余项.md。

CPU模式设置CAPTAIN_CPU_ONLY=1后启动网关；本次10月9日记录来自GPU，没有重新完成CPU评测。模型内容仍可能错误，政治参考答案仍可能缺要点，错误归因尚不能自动入库。

## 文件用途

运行代码包括captain_ai.py、study_context.py、server.py、backend_contract.py；不要漏传study_context.py。test_captain.py和test_round2.py为回归测试；verify_delivery.py为真实HTTP验收；start.ps1为启动入口；run_live.py兼容调用新验收入口。

docs包含逐项回复、字段改动和测试状态；reports保存本次实测记录；SHA256SUMS.txt供检查打包时文件完整性。新增运行记录可以不在原校验清单中，修改已有代码后应重新发布版本和校验清单。

交付不等于全系统验收：仍需二号原项目源码、原7题与测试库、前端联调及独立人工评测。当前没有执行数据库恢复、正式库重跑、对外发送或部署到其他电脑。
