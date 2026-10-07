# AI 接口协议对接说明

> 依据：队长（1 号 王凤菲）《AI模块对接回复.docx》
> 接收日期：2026-09-21　　落实日期：2026-09-21
> 落实人：2 号成员（后端 + OCR + AI 接口联动）
> 协议版本号：`2026-09-21-captain-v1`（写在 `ai_interface.PROTOCOL_VERSION`）

---

## 一、队长协议要点（摘录）

| 项 | 协议内容 |
|---|---|
| 模型 | 本机 Ollama；`qwen2.5:7b-instruct-q4_K_M`，**2026-09-27 前**跑通 |
| 请求体 | `{scene, subject, input, user_context}` |
| scene 取值 | `error_analysis` / `qa` / `weak_diagnosis` / `school_recommend` / `essay_review` |
| subject 取值 | `politics` / `math` / `english` / `cet4` / `cet6` |
| 返回体 | `{code, scene, data, raw, model, elapsed_ms}` |
| code 含义 | **0** 成功　**1** 模型异常　**2** 返回不是合法 JSON 或字段不符 |
| error_type | 四选一：`concept_misunderstanding` / `calculation_error` / `misread_question` / `method_gap` |
| 附带字段 | `error_type_label`、`error_confidence`、`error_reason`（一句话判定依据） |
| 置信度规则 | `error_confidence < 0.5` → 打"待人工复核"标签，**不自动入库** |
| 硬约束 | 队长侧用 `format=json` 强制 JSON；**我侧收到后校验字段，code=2 自动重试 1 次，仍失败降级（保留 raw、置 code=1），不得将模型原文直接入库** |

---

## 二、我侧逐条落地情况

| # | 协议要求 | 落地位置 | 状态 |
|---|---|---|---|
| 1 | 请求体四字段 | `AIRequest(scene, subject, input, user_context)` | ✅ |
| 2 | 五个 scene 名 | `SCENE_ALIASES` | ✅ |
| 3 | 旧名兼容 | `explain_wrong→error_analysis`、`diagnose→weak_diagnosis`、`school_advice→school_recommend`、`essay_polish→essay_review` | ✅ |
| 4 | subject 五值 | `SUBJECTS`（含中文名） | ✅ |
| 5 | 返回信封 | `AIResponse.to_envelope()` → `{code, scene, data, raw, model, elapsed_ms}` | ✅ |
| 6 | code=0/1/2 | `call_ai()` 三分支 | ✅ |
| 7 | format=json | `FORCE_JSON = True`，`_call_ollama()` 带 `format` 字段 | ✅ |
| 8 | 字段校验 | `validate(scene, data)`，按 `SCENE_SPECS` 逐个场景校验 | ✅ |
| 9 | 重试 1 次 | `MAX_RETRY = 1` | ✅ |
| 10 | 降级保留 raw | 降级时 `data=None`、`raw={"text": 原文, "reason":..., "retried":1}` | ✅ |
| 11 | 不得原文入库 | 降级时 `content=""`；入库只写 `render_content(data)` 渲染结果 | ✅ |
| 12 | 置信度 < 0.5 待复核 | `need_review` 属性 + `ocr_ai_batch.py` 写库时置 `review_flag='pending_review'`、`error_type=NULL` | ✅ |
| 13 | 四类 code | `ERROR_TYPE_CODES` | ✅ |
| 14 | 历史数据迁移 | `error_classify.py --migrate`（旧 `concept/calculation/misread/method` → 新 code） | ✅ 已迁 2 行 |

### 新增数据库字段（`error_items`）

| 字段 | 类型 | 含义 |
|---|---|---|
| `error_type` | TEXT | 四 code 之一；待复核时为 NULL |
| `error_confidence` | REAL | 0~1 |
| `error_reason` | TEXT | 一句话判定依据 |
| `review_flag` | TEXT | `pending_review` 或 NULL |
| `error_type_conf` | TEXT | 旧分级字段，保留（high/low/template/pending_review） |

---

## 三、改造涉及的文件

| 文件 | 改动 |
|---|---|
| `src/backend/ai_interface.py` | 整体重写：协议常量、场景别名、字段校验、JSON 解析、重试降级、渲染 |
| `src/backend/error_classify.py` | 四类 code、置信度数值化、`error_reason`、待复核逻辑、旧值迁移 |
| `src/backend/ocr_ai_batch.py` | 只在 `code==0` 入库；写置信度与待复核标记；失败原文只进 `ai_logs` |
| `src/backend/study_stats.py` | 饼图/雷达图改用新 code（兼容旧值）；新增 `pending_review` 计数 |
| `src/backend/api.py` | `/errors` 列表新增强度字段 `error_confidence` / `error_reason` / `review_flag` |
| `src/backend/db/schema.sql` | `error_items` 补 4 个新字段 |
| `src/backend/api_smoke_test.py` | 新增跑前快照、跑后自动清理（不再污染统计数据） |
| `src/backend/ai_protocol_test.py` | **新增**：协议联调自测（桩函数，不依赖真模型） |

---

## 四、实测结果（重要，需反馈队长）

按新规则对库里现有 **7 道**已解析错题重新分类：

| 结果 | 数量 | 说明 |
|---|---|---|
| 自动入库 | **1 道**（id=13，计算失误，conf=0.6） | 精确命中类别词 |
| 待人工复核 | **6 道** | 其中 5 道 conf=0（模型在背模板），1 道 conf=0.4（仅宽松词） |

**这 6 道为什么不合格**：现有解析是 0.5b 在**自由文本模式**下产出的，模型习惯把四类一起列举出来当模板背（例如"错误原因归类：概念不清，计算失误，审题偏差，方法缺失"）。这不是分类，是背书。新协议强制 `format=json` 后，模型必须四选一，理论上能大幅改善。

**结论**：我侧已具备协议要求的全部校验与降级能力，但**当前 0.5b 的解析质量不足以支撑自动入库**。等 7b 到位后重跑，才有比较意义。

---

## 五、验证方式（可复现）

```bash
# 1. 协议层自测（不需要模型在线）
venv\Scripts\python.exe src/backend/ai_interface.py

# 2. 协议联调自测：桩函数模拟 8 种边界（不需要模型在线）
venv\Scripts\python.exe src/backend/ai_protocol_test.py

# 3. 分类演练 / 落库 / 旧值迁移
venv\Scripts\python.exe src/backend/error_classify.py
venv\Scripts\python.exe src/backend/error_classify.py --apply
venv\Scripts\python.exe src/backend/error_classify.py --migrate
```

`ai_protocol_test.py` 覆盖的 8 种情况：
1. 合法 JSON 一次通过 → code=0
2. 首次散文、二次合法 → 重试生效，code=0
3. 两次都不合法 → code=1 降级，data=None，raw 保留原文
4. 缺必填字段 → code=1，error 指明缺什么
5. `error_type` 不在四 code 内 → code=1
6. 置信度 0.3 → `need_review=True`
7. 服务不可达 → code=1 且不抛异常（跑批不中断）
8. 旧函数名仍可调用（`ok`/`content`/`elapsed_ms` 兼容）

---

## 六、待队长确认 / 待办

| # | 问题 | 影响 |
|---|---|---|
| 1 | 7b 到位后，是后端**直连本机 Ollama**（改 `MODEL_NAME`），还是队长另起 HTTP 服务（填 `AI_GATEWAY_URL`）？ | 两种我侧都已预留开关 `AI_PROVIDER`，告知即可切换 |
| 2 | `judge_fragment`（切分漏题兜底）是我侧扩展场景，不在队长五场景内，是否认可沿用同一信封？ | 不认可我就单独走一路，不影响主协议 |
| 3 | 择校 `school_recommend`：v1 先做占位，**需要 4 号的院校表字段**才能出真实推荐 | 等 4 号 |
| 4 | `user_context` 里队长希望我传哪些学情字段？ | 我侧默认传 `study_stats` 的 `summary`，可按需扩 |
| 5 | 7b 到位后需要重跑分类与诊断，重跑口径（是否 `--clear`）请队长定 | 我倾向 `--clear` 全量重跑，保证口径一致 |


---

# 七、2026-09-27 队长回复后的增量（更新于 2026-10-07）

队长《对AI接口协议回复》已下发，第六节的 5 个待确认问题全部有答复。
下面只记**相对本文档前文发生变化**的部分，未变的照旧。

## 7.1 接入方式：走队长网关，不是直连

| 项 | 值 |
|---|---|
| 接口 | `POST http://127.0.0.1:8765/ai` |
| 切换 | `AI_PROVIDER` / `AI_GATEWAY_URL` / `AI_GATEWAY_TIMEOUT` 三个环境变量 |
| 超时 | 网关单次 180s × 最多 2 次 → 后端等候 370s |
| 启动 | 队长交付包 `start.ps1`（交付包我侧尚未收到） |

⚠️ `127.0.0.1` 指**调用者所在电脑**：联调时本机要同时跑 Ollama 和队长网关；
当前未开放局域网，不能拿这个地址跨电脑访问。

## 7.2 ⚠️ code=2 语义变了（本文档第二节的代码说明已过时）

| 分支 | code=2 含义 | 重试归属 |
|---|---|---|
| 原协议 / local 直连 | 模型输出不合法 | 后端重试 1 次 |
| **队长网关** | **请求字段错误** | 网关内部已重试；后端**不重试**，直接反馈调用方 |

**信封结构相同 ≠ 语义相同**。我侧用 `CODE_SEMANTICS` 记录两套语义，
并在返回信封加 `protocol` 字段显式声明当前走哪套，避免前端按旧口径误判。

队长要求："同一版本名称不能对应两套 code 语义，核对通过后再更新协议版本
或增加能力版本。" → 已按分支给不同版本标识。

## 7.3 qa 子任务（六项功能映射）

| 功能 | scene | user_context.task | 必填字段 |
|---|---|---|---|
| 错题解析 | error_analysis | 不需要 | 见第三节 |
| 数学指导 | qa | math_guidance | answer, steps |
| 政治抽查 | qa | politics_quiz | verdict（输入走 input.student_answer） |
| 作文润色 | essay_review | 不需要 | corrected, issues |
| 薄弱诊断 | weak_diagnosis | 不需要，传 summary | weak_points, advice |
| 复习规划 | qa | study_plan | plan |

补充字段：`available_minutes_per_day`（1~480，默认 60）、
`plan_days`（1~7，默认 3），越界夹取不报错。

⚠️ 上述 qa 字段名是**我侧提案**，以队长交付包 `schemas.json` 为准。

## 7.4 待复核口径（本文档第三节"置信度<0.5 不入库"已升级）

队长：本轮**所有 AI 归因交人工复核**，不因置信度高就入库。
开关 `AUTO_COMMIT_CLASSIFICATION=False`（ai_interface 与 error_classify 共用）。

排除规则（已写进 `GET /stats` 的 `error.review_policy`，前端可读）：
- 排除：四类分布（待复核单列「待复核（未确认）」桶）、雷达图计算维度、
  任何基于 error_type 的推荐
- 包含：错题总数、科目/题型分布、正确率、薄弱考点（来自人工标签不是 AI 分类）

## 7.5 择校口径变更

未接院校库时返回 **code=1、data=null、raw.reason=school_data_unavailable**
（原为 code=0 返回空三档）。队长："不能把占位结果包装成成功推荐。"

## 7.6 judge_fragment

队长同意保留我侧独立处理路径，**单独定义和校验 data，不混入五主场景**。
请求/成功/失败三示例见 `docs/回复队长_第二轮反馈.md` 第 3 节。
切分判断失败时保留待检查状态，不据此丢弃疑似题目。

## 7.7 7B 重跑

不做 `--clear`。流程：备份（校验行数一致）→ 测试库 → 跑 7B → 对照表。
工具 `src/backend/ai_recompare_7b.py`，正式库只读。
