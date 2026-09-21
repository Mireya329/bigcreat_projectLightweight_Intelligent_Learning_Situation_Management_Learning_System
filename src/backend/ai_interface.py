# -*- coding: utf-8 -*-
"""AI 接口层（后端 ⇄ 本地大模型的唯一通道）

==============================================================
协议版本：2026-09-21 队长《AI模块对接回复》v1
==============================================================
本模块于 2026-09-21 按队长下发的对接协议改造。三条硬约束已落地：

1. **请求体**：{scene, subject, input, user_context}
2. **返回体**：{code, scene, data, raw, model, elapsed_ms}
     code = 0 成功 / 1 模型异常或降级 / 2 返回不是合法 JSON 或字段不符
3. **数据纪律**：code=2 自动重试 1 次；仍失败则降级（保留 raw、置 code=1、
     data=null），**绝不允许把模型原文直接写进数据库**。

场景名（队长协议五场景）
------------------------
    error_analysis    错题智能解析     （旧名 explain_wrong / explain_wrong_v2）
    qa                知识点问答
    weak_diagnosis    薄弱点诊断       （旧名 diagnose）
    school_recommend  择校冲稳保推荐   （旧名 school_advice，v1 仅占位）
    essay_review      作文批改         （旧名 essay_polish）

    judge_fragment    本侧扩展场景（模块六：切分漏题兜底），不在队长五场景内，
                      沿用同一套返回结构，便于统一处理。

错误类型（四个 code，队长协议冻结）
----------------------------------
    concept_misunderstanding  概念理解错误
    calculation_error         计算失误
    misread_question          审题偏差
    method_gap                方法缺失

    error_confidence < 0.5 → 打"待人工复核"标签，不自动入库（由调用方执行）

向后兼容
--------
旧调用方（ocr_ai_batch.py / ocr_recover.py / ai_diagnose.py）用的
`resp.ok` / `resp.content` / `resp.elapsed_ms` 三个属性全部保留，
旧场景名走别名映射，因此改造不需要改动它们的调用代码。

新增的属性：code / data / raw / model / error / to_envelope()

使用方法
--------
    python ai_interface.py              # 内置自测（离线，不依赖 Ollama）
    python ai_interface.py --live       # 真机自测（需 Ollama 在线）

记录规范
--------
每次联调报错请记入 docs/第二阶段错误日志.md（命令原文 + 报错原文 + 解决办法）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Optional

# ============================================================
# 配置区
# ============================================================
MOCK_MODE = False                      # True=返回占位数据；False=真实调用
OLLAMA_BASE_URL = "http://localhost:11434"
MODEL_NAME = "qwen2.5:0.5b"            # 当前仍是 0.5b；队长承诺 2026-09-27 前换 7b
MODEL_NAME_7B = "qwen2.5:7b-instruct-q4_K_M"   # 队长部署后替换 MODEL_NAME 为此值
REQUEST_TIMEOUT = 120                  # 本地 CPU 推理较慢，超时给足

# 队长协议硬约束：用 Ollama format=json 强制模型只输出 JSON
FORCE_JSON = True
# 队长协议：code=2（格式不符）时自动重试 1 次，仍失败才降级
MAX_RETRY = 1
# 置信度阈值：低于此值打"待人工复核"，不自动入库
CONFIDENCE_THRESHOLD = 0.5

# 稳定性校验：同一题多跑几次，分类一致才信。
#   背景（2026-09-21 真机实测）：0.5b 分类不可复现，5 题 × 3 次仅 1/5 一致，
#   且模型会在随机分类上给满把握，所以 conf<0.5 这道闸门拦不住。
#   开启后：一致 → 照常返回；不一致 → 保留解析但把置信度置 0，
#   让它自动落进"待人工复核"（复用现有规则，不新造机制）。
#   代价是推理次数翻倍，7b 到位后若实测稳定可关掉。
STABILITY_CHECK = False
STABILITY_ROUNDS = 2                   # 跑几轮（≥2 才有比较意义）

# 队长的模型提供方：
#   "local"   直连本机 Ollama（默认，当前阶段）
#   "gateway" 队长部署 7b 后若另起 HTTP 服务，填 AI_GATEWAY_URL 并改这里
AI_PROVIDER = "local"
AI_GATEWAY_URL = ""                    # 例："http://localhost:8000/ai"


# ============================================================
# 协议常量
# ============================================================
PROTOCOL_VERSION = "2026-09-21-captain-v1"

SUBJECTS = {                           # 队长协议允许的学科枚举
    "politics": "政治",
    "math": "数学",
    "english": "英语",
    "cet4": "四级",
    "cet6": "六级",
}
DEFAULT_SUBJECT = "math"

# 库内 subjects.code → 队长协议 subject
# 送 AI 时必须按题目实际学科传，不能一律用默认值，否则政治题会被当成数学题解析
SUBJECT_CODE_MAP = {
    "POSTGRAD_POLITICS": "politics",
    "POSTGRAD_MATH": "math",
    "POSTGRAD_ENGLISH": "english",
    "CET4": "cet4",
    "CET6": "cet6",
}


def subject_from_code(code: Optional[str]) -> str:
    """把库里的科目 code 转成队长协议的 subject。

    认不出来时退回默认值，并**打印一行告警**——静默兜底最危险，
    宁可吵一点，也不能让政治题悄悄按数学解析了。
    """
    if not code:
        return DEFAULT_SUBJECT
    hit = SUBJECT_CODE_MAP.get(code.strip().upper())
    if hit:
        return hit
    # 已经是协议取值（politics/math/...）就直接放行
    low = code.strip().lower()
    if low in SUBJECTS:
        return low
    print(f"[警告] 科目 code {code!r} 无法映射到协议 subject，"
          f"本次退回 {DEFAULT_SUBJECT!r}。请把映射补进 SUBJECT_CODE_MAP。")
    return DEFAULT_SUBJECT

ERROR_TYPE_CODES = {                   # 四个 code，队长协议冻结，不可自行增删
    "concept_misunderstanding": "概念理解错误",
    "calculation_error": "计算失误",
    "misread_question": "审题偏差",
    "method_gap": "方法缺失",
}

# 旧取值 → 队长新 code（用于把库里已有的历史数据迁过来）
LEGACY_ERROR_TYPE_MAP = {
    "concept": "concept_misunderstanding",
    "calculation": "calculation_error",
    "misread": "misread_question",
    "method": "method_gap",
    # 大小写变体（schema.sql 注释里曾写过 Concept/Calculation 之类）
    "concept_misunderstanding": "concept_misunderstanding",
    "calculation_error": "calculation_error",
    "misread_question": "misread_question",
    "method_gap": "method_gap",
    "conceptmisunderstanding": "concept_misunderstanding",
}

# 旧场景名 → (队长协议场景名, Prompt 变体)
SCENE_ALIASES: dict[str, tuple[str, Optional[str]]] = {
    "explain_wrong":     ("error_analysis", None),
    "explain_wrong_v2":  ("error_analysis", "v2"),
    "error_analysis":    ("error_analysis", None),
    "error_analysis_v2": ("error_analysis", "v2"),
    "qa":                ("qa", None),
    "diagnose":          ("weak_diagnosis", None),
    "weak_diagnosis":    ("weak_diagnosis", None),
    "school_advice":     ("school_recommend", None),
    "school_recommend":  ("school_recommend", None),
    "essay_polish":      ("essay_review", None),
    "essay_review":      ("essay_review", None),
    "judge_fragment":    ("judge_fragment", None),   # 本侧扩展
}


# ============================================================
# 数据结构
# ============================================================
@dataclass
class AIRequest:
    """统一请求体，字段与队长协议一致：scene / subject / input / user_context"""
    scene: str
    input: dict = field(default_factory=dict)
    subject: str = DEFAULT_SUBJECT
    user_context: Optional[dict] = None
    variant: Optional[str] = None       # Prompt A/B 变体（如 error_analysis 的 v2）
    # --- 旧字段，仅为了向后兼容，等价于 input / user_context ---
    payload: Optional[dict] = None
    context: Optional[dict] = None

    def __post_init__(self):
        if not self.input and self.payload:
            self.input = self.payload
        if self.user_context is None and self.context is not None:
            self.user_context = self.context
        if self.subject not in SUBJECTS:
            # 不阻断，只归一：未知学科退回默认，避免脏值流到模型侧
            self.subject = DEFAULT_SUBJECT

    def to_protocol(self) -> dict:
        """序列化成队长协议规定的请求体（发给 gateway / 记日志用）"""
        return {
            "scene": self.scene,
            "subject": self.subject,
            "input": self.input,
            "user_context": self.user_context or {},
        }


@dataclass
class AIResponse:
    """统一返回体。

    code: 0=成功  1=模型异常或降级  2=返回格式不符（内部重试后仍失败则为 1）
    data: 校验通过的结构化结果；失败时为 None（**绝不**把模型原文放这里）
    raw:  模型原始输出，只用于排查，禁止直接入库
    """
    ok: bool
    scene: str
    content: str                        # 由 data 渲染的可读文本（失败时为空串）
    raw: Optional[dict] = None
    elapsed_ms: int = 0
    code: int = 0
    data: Optional[dict] = None
    model: str = ""
    error: Optional[str] = None         # 失败原因
    retries: int = 0                    # 实际重试次数（0=一次通过）

    def to_envelope(self) -> dict:
        """队长协议返回体：{code, scene, data, raw, model, elapsed_ms}"""
        return {
            "code": self.code,
            "scene": self.scene,
            "data": self.data,
            "raw": self.raw,
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
        }

    @property
    def need_review(self) -> bool:
        """置信度不足 → 待人工复核（队长规则：error_confidence < 0.5）"""
        if not self.data:
            return True
        conf = self.data.get("error_confidence")
        try:
            return float(conf) < CONFIDENCE_THRESHOLD
        except (TypeError, ValueError):
            return True


# ============================================================
# 场景契约：每个场景 data 里必须有哪些字段
# ============================================================
@dataclass(frozen=True)
class SceneSpec:
    required: tuple[str, ...]
    optional: tuple[str, ...] = ()


SCENE_SPECS: dict[str, SceneSpec] = {
    "error_analysis": SceneSpec(
        required=("solution", "error_type", "error_type_label",
                  "error_confidence", "error_reason", "knowledge_points",
                  "similar_question"),
    ),
    "qa": SceneSpec(
        required=("answer",),
        optional=("related_points",),
    ),
    "weak_diagnosis": SceneSpec(
        required=("weak_points", "advice"),
        optional=("priority",),
    ),
    "school_recommend": SceneSpec(
        required=("reach", "match", "safety"),
        optional=("note",),
    ),
    "essay_review": SceneSpec(
        required=("corrected", "issues"),
        optional=("score", "comments"),
    ),
    "judge_fragment": SceneSpec(
        required=("choice",),
        optional=("guessed_no", "stem", "qtype", "reason"),
    ),
}


# ============================================================
# Prompt 模板
# ============================================================
_JSON_RULE = ("\n【输出格式】只输出一个 JSON 对象，不要任何解释、不要 Markdown 代码围栏。\n"
              "字段要求：\n{contract}")

PROMPT_TEMPLATES: dict[str, str] = {
    # ---------- 模块一：错题智能解析（队长协议：error_analysis）----------
    "error_analysis": (
        "你是一名大学辅导老师。以下是学生做错的一道题，请给出：\n"
        "1. 正确解题思路\n"
        "2. 错误原因归类（只能从四个 code 中选一个）\n"
        "3. 关联知识点\n"
        "4. 一道同类变式题\n\n"
        "学科：{subject_cn}\n题目：{question}\n学生答案：{student_answer}"
    ),
    # v2（2026-09-15 A/B 实验）：增加"OCR 残缺识别"约束
    # 背景：题干残缺时模型会一本正经地编造解析（垃圾进、幻觉出），
    # v2 在完全保留 v1 结构的前提下要求模型"看不清就说看不清"。
    "error_analysis_v2": (
        "你是一名大学辅导老师。以下是学生做错的一道题，请给出：\n"
        "1. 正确解题思路\n"
        "2. 错误原因归类（只能从四个 code 中选一个）\n"
        "3. 关联知识点\n"
        "4. 一道同类变式题\n\n"
        "【重要前提】题目文本来自 OCR 扫描识别，可能残缺、含乱码或顺序错乱。\n"
        "如果发现题目不完整、含明显乱码（如 Ox100001、f(xbsy6 之类）或无法理解题意，\n"
        "你必须把 error_type 判为 method_gap，error_confidence 给 0，\n"
        "solution 直接写\"题目文本残缺，无法解析\"，similar_question 填空字符串。\n"
        "严禁猜测题意、严禁编造解答和变式题。\n\n"
        "学科：{subject_cn}\n题目：{question}\n学生答案：{student_answer}"
    ),
    # ---------- 模块二：知识点问答 ----------
    "qa": "你是知识点答疑助手，用中文简洁准确地回答。\n学科：{subject_cn}\n问题：{question}",
    # ---------- 模块三：薄弱点诊断 ----------
    "weak_diagnosis": (
        "根据以下学习数据（正确率、错题分布、复习记录），诊断薄弱知识点并给出复习建议。\n"
        "学科：{subject_cn}\n学习数据：{stats_json}"
    ),
    # ---------- 模块四：择校（v1 仅占位，等 4 号院校表）----------
    "school_recommend": (
        "根据学生情况（本科院校/专业/目标分数/意向地区），按冲、稳、保三档各推荐院校。\n"
        "注意：当前院校库尚未接入，如无可靠依据请返回空列表并在 note 中说明。\n"
        "学生情况：{profile}"
    ),
    # ---------- 模块五：作文批改 ----------
    "essay_review": (
        "请批改以下英语作文：指出语法错误、给出润色后的全文。\n"
        "学科：{subject_cn}\n作文：{essay}"
    ),
    # ---------- 模块六：碎片归属判断（本侧扩展）----------
    "judge_fragment": (
        "以下是试卷 OCR 文本的一个片段，它位于两道已识别的题目之间，题号可能已丢失。\n"
        "请判断：A. 它是上一题的延续（作答过程、笔记、公式推导）\n"
        "        B. 它是一道独立的题目\n"
        "        C. 无法判断（文本残缺到看不清）\n"
        "规矩：文本若残缺到看不清，选 C，不要猜测。若判断为 B，给出题号猜测、题干、题型。\n\n"
        "上一题末尾：{prev}\n下一题开头：{next}\n待判断片段：{fragment}"
    ),
}

# 每个场景给模型的 JSON 字段说明（拼进 prompt，配合 format=json）
FIELD_HINTS: dict[str, str] = {
    "error_analysis": (
        '- "solution": 字符串，正确解题思路\n'
        '- "error_type": 只能是 concept_misunderstanding / calculation_error / '
        'misread_question / method_gap 之一\n'
        '- "error_type_label": 中文标签（概念理解错误/计算失误/审题偏差/方法缺失）\n'
        '- "error_confidence": 0 到 1 之间的小数，表示你对这个判定的把握\n'
        '- "error_reason": 一句话说明判定依据\n'
        '- "knowledge_points": 字符串数组，关联知识点\n'
        '- "similar_question": 字符串，一道同类变式题'
    ),
    "qa": (
        '- "answer": 字符串，回答正文\n'
        '- "related_points": 字符串数组，相关知识点（可选）'
    ),
    "weak_diagnosis": (
        '- "weak_points": 字符串数组，薄弱知识点\n'
        '- "advice": 字符串，复习建议\n'
        '- "priority": 字符串数组，按优先级排序的知识点（可选）'
    ),
    "school_recommend": (
        '- "reach": 字符串数组，冲档院校\n'
        '- "match": 字符串数组，稳档院校\n'
        '- "safety": 字符串数组，保档院校\n'
        '- "note": 字符串，说明（可选）'
    ),
    "essay_review": (
        '- "corrected": 字符串，润色后的全文\n'
        '- "issues": 字符串数组，逐条指出问题\n'
        '- "score": 数字，评分（可选）\n'
        '- "comments": 字符串，总评（可选）'
    ),
    "judge_fragment": (
        '- "choice": 只能是 "A" / "B" / "C"\n'
        '- "guessed_no": 字符串，题号猜测（选 B 时填）\n'
        '- "stem": 字符串，题干摘录（选 B 时填）\n'
        '- "qtype": 字符串，题型（选 B 时填）\n'
        '- "reason": 字符串，一句话理由'
    ),
}

# 占位数据（MOCK_MODE 或 Ollama 离线时使用）
MOCK_DATA: dict[str, dict] = {
    "error_analysis": {
        "solution": "[占位] 第 2 阶段接入 Ollama 后返回真实解析。",
        "error_type": "method_gap",
        "error_type_label": "方法缺失",
        "error_confidence": 0.0,
        "error_reason": "占位数据，非真实判定",
        "knowledge_points": [],
        "similar_question": "",
    },
    "qa": {"answer": "[占位] 知识点问答结果。", "related_points": []},
    "weak_diagnosis": {"weak_points": [], "advice": "[占位] 薄弱点诊断结果。"},
    "school_recommend": {"reach": [], "match": [], "safety": [], "note": "[占位] 院校库未接入。"},
    "essay_review": {"corrected": "[占位] 作文批改结果。", "issues": []},
    "judge_fragment": {"choice": "C", "reason": "占位数据"},
}

# 旧调用方可能直接读 MOCK_OUTPUTS，保留一个兼容视图
MOCK_OUTPUTS: dict[str, str] = {k: "[占位] " + k for k in MOCK_DATA}


# ============================================================
# 底层：调用模型
# ============================================================
def _call_ollama(prompt: str, force_json: bool = True) -> str:
    """调用本机 Ollama /api/chat。

    ⚠️ 这是本项目的 AI 出口之一（另一个是 gateway 分支）。业务代码禁止绕过
    本模块直连模型，方便统一换模型、加缓存、记日志。
    """
    body: dict[str, Any] = {
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    if force_json and FORCE_JSON:
        # 队长硬约束：强制模型只输出 JSON
        body["format"] = "json"

    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data.get("message", {}).get("content", "")


def _call_gateway(scene: str, subject: str, input_: dict,
                  user_context: dict) -> str:
    """队长若另起 HTTP 服务，走这个分支（请求体与队长协议完全一致）。"""
    body = json.dumps({
        "scene": scene,
        "subject": subject,
        "input": input_,
        "user_context": user_context,
    }).encode("utf-8")
    req = urllib.request.Request(
        AI_GATEWAY_URL, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    # gateway 返回的就是协议信封，把 data 原样序列化回文本交给解析层
    return json.dumps(data.get("data", {}), ensure_ascii=False)


def check_ollama_alive() -> bool:
    """部署检查：探测模型服务是否在线（对接组长时先用这个）。"""
    try:
        if AI_PROVIDER == "gateway" and AI_GATEWAY_URL:
            urllib.request.urlopen(AI_GATEWAY_URL, timeout=3)
            return True
        with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=3):
            return True
    except Exception:
        return False


# ============================================================
# 解析与校验
# ============================================================
def extract_json(text: str) -> tuple[Optional[dict], str]:
    """从模型输出里抠出 JSON 对象。

    模型常犯的毛病：外面套 ```json 围栏、前面加"好的，以下是"、后面加解释。
    返回 (解析结果, 原始文本)；解析不出来时结果为 None。
    """
    if not text:
        return None, text or ""

    s = text.strip()
    # 1. 直接就是 JSON
    try:
        obj = json.loads(s)
        return (obj if isinstance(obj, dict) else None), text
    except json.JSONDecodeError:
        pass

    # 2. 去掉 Markdown 围栏再试
    fenced = re.sub(r"^```(?:json)?\s*|\s*```$", "", s, flags=re.MULTILINE).strip()
    if fenced != s:
        try:
            obj = json.loads(fenced)
            if isinstance(obj, dict):
                return obj, text
        except json.JSONDecodeError:
            pass

    # 3. 截取第一个 { 到最后一个 }
    i, j = s.find("{"), s.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(s[i:j + 1])
            if isinstance(obj, dict):
                return obj, text
        except json.JSONDecodeError:
            pass

    return None, text


def validate(scene: str, data: Any) -> tuple[bool, str]:
    """校验 data 是否满足场景契约。返回 (是否通过, 不通过的原因)。"""
    spec = SCENE_SPECS.get(scene)
    if spec is None:
        return False, f"未知 scene: {scene}"

    if not isinstance(data, dict):
        return False, "data 不是 JSON 对象"

    missing = [k for k in spec.required
               if k not in data or data[k] in (None, "", [])]
    # similar_question 允许为空串（题目残缺时模型本来就该留空）
    missing = [k for k in missing if k != "similar_question"]
    if missing:
        return False, f"缺少或空字段: {missing}"

    if scene == "error_analysis":
        et = str(data.get("error_type", "")).strip().lower()
        if et not in ERROR_TYPE_CODES:
            return False, f"error_type 不在四个 code 内: {data.get('error_type')!r}"
        try:
            conf = float(data.get("error_confidence"))
        except (TypeError, ValueError):
            return False, f"error_confidence 不是数值: {data.get('error_confidence')!r}"
        if not 0.0 <= conf <= 1.0:
            return False, f"error_confidence 超出 0~1: {conf}"
        if not isinstance(data.get("knowledge_points"), list):
            return False, "knowledge_points 必须是数组"
        # 模型若判了"概念理解错误"却把四类全写进理由里，判为背书
        reason = str(data.get("error_reason", ""))
        hits = sum(1 for w in ("概念不清", "计算失误", "审题偏差", "方法缺失")
                   if w in reason)
        if hits >= 3:
            return False, "error_reason 在列举四类（模型在背模板，未真分类）"

    if scene == "judge_fragment":
        ch = str(data.get("choice", "")).strip().upper()[:1]
        if ch not in ("A", "B", "C"):
            return False, f"choice 只能是 A/B/C: {data.get('choice')!r}"

    return True, ""


def normalize_error_analysis(data: dict) -> dict:
    """以 error_type 为准，修正模型乱给的 error_type_label。

    真机实测（2026-09-21，0.5b）发现模型会把 label 填成：
      - 自造长句（"区间外函数与区间内函数的函数关系"）
      - 与 code 错配（method_gap 却写"计算失误"）
    协议里 label 是给人看的，必须与 code 一一对应，所以由我侧按映射表覆盖，
    不信模型的。同时把 confidence 收敛成 float，避免 1 / "0.8" 混着存。
    """
    out = dict(data)
    et = str(out.get("error_type", "")).strip().lower()
    out["error_type"] = et
    out["error_type_label"] = ERROR_TYPE_CODES.get(et, "")
    try:
        out["error_confidence"] = round(float(out.get("error_confidence")), 3)
    except (TypeError, ValueError):
        out["error_confidence"] = 0.0
    kp = out.get("knowledge_points")
    out["knowledge_points"] = [str(x) for x in kp] if isinstance(kp, list) else []
    return out


def render_content(scene: str, data: dict) -> str:
    """把结构化 data 渲染成人能读的文本。

    入库的是这个渲染结果，不是模型原文——这是队长"不得原文入库"的落点。
    """
    if scene == "error_analysis":
        kp = data.get("knowledge_points") or []
        kp_s = "、".join(str(x) for x in kp) if kp else "（未给出）"
        sq = data.get("similar_question") or "（未给出）"
        return (
            f"【正确解题思路】\n{data.get('solution', '')}\n\n"
            f"【错误原因】{data.get('error_type_label', '')}"
            f"（{data.get('error_type', '')}，置信度 {data.get('error_confidence', 0)}）\n"
            f"判定依据：{data.get('error_reason', '')}\n\n"
            f"【关联知识点】{kp_s}\n\n"
            f"【同类变式题】\n{sq}"
        )
    if scene == "qa":
        return str(data.get("answer", ""))
    if scene == "weak_diagnosis":
        wp = data.get("weak_points") or []
        wp_s = "\n".join(f"  - {x}" for x in wp) or "  （未给出）"
        return f"【薄弱知识点】\n{wp_s}\n\n【复习建议】\n{data.get('advice', '')}"
    if scene == "school_recommend":
        def fmt(name, items):
            items = items or []
            return f"【{name}】\n" + ("\n".join(f"  - {x}" for x in items)
                                    if items else "  （未给出）")
        out = (fmt("冲", data.get("reach")) + "\n\n"
               + fmt("稳", data.get("match")) + "\n\n"
               + fmt("保", data.get("safety")))
        if data.get("note"):
            out += f"\n\n【说明】{data['note']}"
        return out
    if scene == "essay_review":
        issues = data.get("issues") or []
        iss_s = "\n".join(f"  {n}. {x}" for n, x in enumerate(issues, 1)) or "  （无）"
        out = f"【润色后全文】\n{data.get('corrected', '')}\n\n【问题清单】\n{iss_s}"
        if data.get("comments"):
            out += f"\n\n【总评】{data['comments']}"
        return out
    if scene == "judge_fragment":
        ch = str(data.get("choice", "")).strip().upper()[:1]
        extra = ""
        if ch == "B":
            extra = (f"\n题号猜测：{data.get('guessed_no', '未知')}"
                     f"\n题干：{data.get('stem', '')}"
                     f"\n题型：{data.get('qtype', '未知')}")
        return f"{ch}{extra}\n理由：{data.get('reason', '')}"
    return json.dumps(data, ensure_ascii=False, indent=2)


def _safe_format(tpl: str, mapping: dict) -> str:
    """模板渲染：占位符缺失时填空串，不让 KeyError 打断流程。"""
    out = tpl
    for key in re.findall(r"\{(\w+)\}", tpl):
        out = out.replace("{%s}" % key, str(mapping.get(key, "")))
    return out


# ============================================================
# 统一入口
# ============================================================
def call_ai(req: AIRequest) -> AIResponse:
    """所有 AI 功能经此调用。

    流程：场景归一 → 拼 Prompt → 调模型 → 解析 JSON → 校验
         → 不过则重试 1 次 → 仍不过则降级（code=1，data=None，保留 raw）
    """
    if req.scene not in SCENE_ALIASES:
        raise ValueError(f"未知 scene: {req.scene}，可选: {sorted(SCENE_ALIASES)}")

    scene, variant = SCENE_ALIASES[req.scene]
    if req.variant:
        variant = req.variant
    tpl_key = f"{scene}_{variant}" if variant else scene
    if tpl_key not in PROMPT_TEMPLATES:
        tpl_key = scene

    t0 = time.time()
    model_used = MODEL_NAME

    # ---- 分支一：MOCK 或服务离线，直接给占位结构化数据 ----
    if MOCK_MODE or not check_ollama_alive():
        data = dict(MOCK_DATA[scene])
        return AIResponse(
            ok=True, scene=scene, content=render_content(scene, data),
            raw={"mock": True}, elapsed_ms=int((time.time() - t0) * 1000),
            code=0, data=data, model="mock",
        )

    # ---- 分支二：真实调用 ----
    values = dict(req.input or {})
    values["subject_cn"] = SUBJECTS.get(req.subject, req.subject)
    if req.user_context:
        values.setdefault("user_context",
                          json.dumps(req.user_context, ensure_ascii=False))

    prompt = _safe_format(PROMPT_TEMPLATES[tpl_key], values)
    prompt += _JSON_RULE.format(contract=FIELD_HINTS.get(scene, ""))

    last_raw, last_err = "", ""
    for attempt in range(MAX_RETRY + 1):
        try:
            if AI_PROVIDER == "gateway" and AI_GATEWAY_URL:
                text = _call_gateway(scene, req.subject, req.input or {},
                                     req.user_context or {})
            else:
                text = _call_ollama(prompt)
        except urllib.error.URLError as e:
            # 服务连不上/超时：这属于"模型异常"，没有重试意义
            return AIResponse(
                ok=False, scene=scene, content="", raw={"error": str(e)},
                elapsed_ms=int((time.time() - t0) * 1000), code=1, data=None,
                model=model_used, error=f"调用失败: {e}",
            )
        except Exception as e:                       # noqa: BLE001 兜住所有异常
            return AIResponse(
                ok=False, scene=scene, content="", raw={"error": repr(e)},
                elapsed_ms=int((time.time() - t0) * 1000), code=1, data=None,
                model=model_used, error=f"调用异常: {e!r}",
            )

        parsed, last_raw = extract_json(text)
        if parsed is None:
            last_err = "返回不是合法 JSON 对象"
        else:
            ok, why = validate(scene, parsed)
            if ok:
                if scene == "error_analysis":
                    parsed = normalize_error_analysis(parsed)
                return AIResponse(
                    ok=True, scene=scene, content=render_content(scene, parsed),
                    raw={"text": text}, elapsed_ms=int((time.time() - t0) * 1000),
                    code=0, data=parsed, model=model_used, retries=attempt,
                )
            last_err = why

    # ---- 分支三：重试耗尽 → 降级。保留 raw、置 code=1、data=None ----
    return AIResponse(
        ok=False, scene=scene, content="",
        raw={"text": last_raw, "reason": last_err, "retried": MAX_RETRY},
        elapsed_ms=int((time.time() - t0) * 1000),
        code=1, data=None, model=model_used, retries=MAX_RETRY,
        error=f"格式校验未通过（已重试 {MAX_RETRY} 次）: {last_err}",
    )


def call_ai_stable(req: AIRequest, rounds: int = STABILITY_ROUNDS) -> AIResponse:
    """同题多跑，用"分类是否一致"来判断这次结果能不能信。

    为什么需要这个：真机实测（2026-09-21，0.5b）显示模型会在随机分类上
    给满把握，所以单看 error_confidence 分不出"真有把握"和"抽错了还自信"。
    一致性是唯一能观测到的信号——同一个输入它自己都答不到一处，就没有可信度。

    口径（刻意复用现有规则，不新造机制）：
      一致   → 照常返回，confidence 取多轮的**最小值**（保守）
      不一致 → 解析（solution）保留，但 error_confidence 置 0 并标
               stable=false，于是自动落进"待人工复核"、error_type 不入库

    返回体的 code 语义不变；稳定性信息放在 data["stable"] 和 raw 里。
    """
    results = [call_ai(req) for _ in range(max(2, rounds))]

    good = [r for r in results if r.code == 0 and isinstance(r.data, dict)]
    if not good:
        # 全军覆没：把第一次的错误原样抛回去，不掩盖失败
        return results[0]

    types = {str(r.data.get("error_type", "")) for r in good}
    stable = len(types) == 1
    elapsed = sum(r.elapsed_ms for r in results)

    base = good[0].data
    if stable:
        conf = min(float(r.data.get("error_confidence", 0) or 0) for r in good)
        note = f"{len(good)} 轮分类一致（{types.pop()}），置信度取最小值 {conf}"
    else:
        conf = 0.0
        note = (f"{len(good)} 轮分类不一致（{sorted(types)}），"
                f"置信度置 0，转人工复核")

    merged = dict(base)
    merged["error_confidence"] = round(conf, 3)
    merged["stable"] = stable
    if not stable:
        # 分类作废，但别给用户一个看起来像结论的东西
        merged["error_reason"] = f"[分类未通过稳定性校验] {note}"

    return AIResponse(
        ok=True, scene=good[0].scene,
        content=render_content(good[0].scene, merged),
        raw={"rounds": [{"code": r.code, "data": r.data} for r in results],
             "stability_note": note},
        elapsed_ms=elapsed, code=0, data=merged, model=good[0].model,
        retries=sum(r.retries for r in results),
    )


# ============================================================
# 便捷封装（业务侧调这些，不用拼 scene）
# ============================================================
def analyze_error(question: str, student_answer: str = "",
                  subject: str = DEFAULT_SUBJECT,
                  user_context: Optional[dict] = None,
                  variant: Optional[str] = None,
                  stable: Optional[bool] = None) -> AIResponse:
    """错题智能解析（队长协议 scene=error_analysis）。

    variant="v2"          走带 OCR 残缺识别的 Prompt
    stable=True           同题多跑做分类一致性校验（默认取 STABILITY_CHECK）
    """
    req = AIRequest(
        scene="error_analysis", subject=subject, variant=variant,
        input={"question": question, "student_answer": student_answer},
        user_context=user_context,
    )
    if stable is None:
        stable = STABILITY_CHECK
    return call_ai_stable(req) if stable else call_ai(req)


def explain_wrong_question(question: str, student_answer: str) -> AIResponse:
    """旧名兼容：等价于 analyze_error(variant=None)"""
    return analyze_error(question, student_answer)


def explain_wrong_question_v2(question: str, student_answer: str) -> AIResponse:
    """旧名兼容：等价于 analyze_error(variant="v2")"""
    return analyze_error(question, student_answer, variant="v2")


def knowledge_qa(question: str, subject: str = DEFAULT_SUBJECT,
                 user_context: Optional[dict] = None) -> AIResponse:
    """知识点智能问答 / 背诵抽查"""
    return call_ai(AIRequest(scene="qa", subject=subject,
                             input={"question": question},
                             user_context=user_context))


def diagnose_weakness(stats: dict, subject: str = DEFAULT_SUBJECT,
                      user_context: Optional[dict] = None) -> AIResponse:
    """学习薄弱点诊断（队长协议 scene=weak_diagnosis）"""
    return call_ai(AIRequest(
        scene="weak_diagnosis", subject=subject,
        input={"stats_json": json.dumps(stats, ensure_ascii=False)},
        user_context=user_context))


def school_recommend(profile: dict, subject: str = DEFAULT_SUBJECT,
                     user_context: Optional[dict] = None) -> AIResponse:
    """择校冲稳保推荐（v1 占位，等 4 号院校表）"""
    return call_ai(AIRequest(
        scene="school_recommend", subject=subject,
        input={"profile": json.dumps(profile, ensure_ascii=False)},
        user_context=user_context))


def school_advice(profile: dict) -> AIResponse:
    """旧名兼容"""
    return school_recommend(profile)


def review_essay(essay: str, subject: str = "english",
                 user_context: Optional[dict] = None) -> AIResponse:
    """英语作文批改（队长协议 scene=essay_review）"""
    return call_ai(AIRequest(scene="essay_review", subject=subject,
                             input={"essay": essay},
                             user_context=user_context))


def polish_essay(essay: str) -> AIResponse:
    """旧名兼容"""
    return review_essay(essay)


def judge_fragment(fragment: str, prev: str, next_: str) -> AIResponse:
    """碎片归属判断（本侧扩展场景）：这段 OCR 文本是上一题延续，还是被漏掉的独立题？"""
    return call_ai(AIRequest(scene="judge_fragment",
                             input={"fragment": fragment, "prev": prev, "next": next_}))


# ============================================================
# OCR → AI 衔接
# ============================================================
def ocr_result_to_question(ocr_texts: list[str]) -> str:
    """把 PaddleOCR 输出的多行文本拼成完整题目字符串（当前只做按行拼接）。"""
    return "\n".join(t.strip() for t in ocr_texts if t.strip())


# ============================================================
# 自测：python ai_interface.py [--live]
# ============================================================
def _selftest(live: bool = False) -> int:
    print("=" * 64)
    print(f"AI 接口层自测   协议 {PROTOCOL_VERSION}")
    print(f"MOCK_MODE = {MOCK_MODE}   实时调用 = {live}")
    print(f"模型服务在线: {check_ollama_alive()}")
    print("=" * 64)

    # --- 1. 纯函数级自测，不依赖模型 ---
    print("\n[1] 场景别名映射")
    for old, new in (("explain_wrong", "error_analysis"),
                     ("explain_wrong_v2", "error_analysis"),
                     ("diagnose", "weak_diagnosis"),
                     ("school_advice", "school_recommend"),
                     ("essay_polish", "essay_review")):
        got = SCENE_ALIASES[old][0]
        assert got == new, f"{old} 应映射到 {new}，实际 {got}"
        print(f"  ✓ {old:<18} → {got}")

    print("\n[2] JSON 解析（模型常见的三种脏输出）")
    cases = [
        ('{"answer": "死锁"}', True),
        ('```json\n{"answer": "死锁"}\n```', True),
        ('好的，结果如下：\n{"answer": "死锁"}\n希望有帮助', True),
        ('这只是普通文本，没有 JSON', False),
    ]
    for text, expect_ok in cases:
        obj, _ = extract_json(text)
        flag = "✓" if (obj is not None) == expect_ok else "✗"
        print(f"  {flag} {text[:34]!r:<40} → {'解析成功' if obj else '解析失败'}")
        assert (obj is not None) == expect_ok

    print("\n[3] 字段校验")
    good = {"solution": "先求导数", "error_type": "calculation_error",
            "error_type_label": "计算失误", "error_confidence": 0.8,
            "error_reason": "第二步符号代错", "knowledge_points": ["导数"],
            "similar_question": "求 x^3 的导数"}
    ok, why = validate("error_analysis", good)
    print(f"  ✓ 完整字段            → {'通过' if ok else '拒绝'} {why}")
    assert ok

    bad_cases = [
        ("缺字段", {k: v for k, v in good.items() if k != "solution"}),
        ("error_type 不在四 code 内",
         {**good, "error_type": "concept"}),
        ("confidence 超范围", {**good, "error_confidence": 1.8}),
        ("confidence 非数值", {**good, "error_confidence": "很高"}),
        ("error_reason 在背模板",
         {**good, "error_reason": "概念不清，计算失误，审题偏差"}),
    ]
    for name, bad in bad_cases:
        ok, why = validate("error_analysis", bad)
        print(f"  ✓ {name:<22} → {'通过' if ok else '拒绝'}：{why[:38]}")
        assert not ok, f"{name} 本应被拒绝却通过了"

    print("\n[4] 待人工复核判定（阈值 " + str(CONFIDENCE_THRESHOLD) + "）")
    for conf in (0.9, 0.5, 0.3):
        r = AIResponse(ok=True, scene="error_analysis", content="", code=0,
                       data={**good, "error_confidence": conf})
        tag = "需人工复核" if r.need_review else "自动入库"
        print(f"  confidence={conf:<4} → {tag}")
        assert r.need_review == (conf < CONFIDENCE_THRESHOLD)

    print("\n[5] 降级路径：绝不允许原文进 data")
    degraded = AIResponse(ok=False, scene="error_analysis", content="",
                          raw={"text": "模型胡说八道", "reason": "缺字段"},
                          code=1, data=None, model=MODEL_NAME,
                          error="格式校验未通过")
    env = degraded.to_envelope()
    assert env["code"] == 1 and env["data"] is None, "降级时 data 必须为 None"
    assert env["raw"]["text"] == "模型胡说八道", "raw 必须保留原文供排查"
    print(f"  ✓ 信封 = {json.dumps({k: v for k, v in env.items() if k != 'raw'}, ensure_ascii=False)}")
    print("  ✓ raw 已保留原文，data 为 None，content 为空串 → 原文不会入库")

    print("\n[7] 学科映射：库内 code → 协议 subject（别让政治题按数学解析）")
    for code, expect in (("POSTGRAD_MATH", "math"),
                         ("POSTGRAD_POLITICS", "politics"),
                         ("POSTGRAD_ENGLISH", "english"),
                         ("CET4", "cet4"), ("CET6", "cet6")):
        got = subject_from_code(code)
        print(f"  ✓ {code:<18} → {got}")
        assert got == expect, f"{code} 应映射到 {expect}，实际 {got}"
    # 学科名要真的进到 Prompt 里
    sc = SUBJECTS[subject_from_code("POSTGRAD_POLITICS")]
    p = _safe_format(PROMPT_TEMPLATES["error_analysis"],
                     {"question": "q", "student_answer": "a", "subject_cn": sc})
    assert "政治" in p, "学科没有注入 Prompt"
    print(f"  ✓ Prompt 已注入学科：…{'学科：' + sc}…")

    print("\n[8] 渲染（入库文本由 data 生成，非模型原文）")
    print("  " + render_content("error_analysis", good).splitlines()[0])

    print("\n[9] label 归一：以 error_type 为准，不信模型的 label")
    # 真机实测（2026-09-21）模型给过的三种错配
    for et, bad_label, expect in (
            ("misread_question", "函数定义错误", "审题偏差"),
            ("method_gap", "计算失误", "方法缺失"),
            ("method_gap", "区间外函数与区间内函数的函数关系", "方法缺失")):
        n = normalize_error_analysis(
            {"error_type": et, "error_type_label": bad_label,
             "error_confidence": "0.85", "knowledge_points": "不是数组"})
        got = n["error_type_label"]
        print(f"  ✓ {et} + 「{bad_label}」 → {got}")
        assert got == expect, f"{et} 的 label 应为 {expect}，实际 {got}"
        assert n["error_confidence"] == 0.85, "confidence 应收敛成 float"
        assert n["knowledge_points"] == [], "非数组 knowledge_points 应清空"
    upper = normalize_error_analysis({"error_type": "CALCULATION_ERROR"})
    assert upper["error_type"] == "calculation_error", "code 应归一小写"
    print("  ✓ 大小写变体归一：CALCULATION_ERROR → calculation_error")

    # --- 2. 真机自测 ---
    if live:
        print("\n[10] 实时调用")
        if not check_ollama_alive():
            print("  ⚠️ 模型服务不在线，跳过（先启动 Ollama）")
            return 0
        for name, resp in [
            ("错题解析", analyze_error("简述哈希表的冲突解决方法", "不会", subject="math")),
            ("知识点问答", knowledge_qa("什么是操作系统中的死锁？")),
            ("薄弱点诊断", diagnose_weakness({"math_correct_rate": 0.42})),
            ("择校推荐", school_recommend({"target": "计算机专硕", "score": 320})),
            ("作文批改", review_essay("I goes to school yesterday.")),
        ]:
            env = resp.to_envelope()
            state = {0: "成功", 1: "降级", 2: "格式不符"}[env["code"]]
            print(f"  {name:<6} code={env['code']}（{state}） "
                  f"{env['elapsed_ms']}ms {resp.error or ''}")
            if env["code"] == 0:
                print("    " + resp.content.splitlines()[0][:56])
    else:
        print("\n[10] 实时调用：跳过（加 --live 开启，需模型服务在线）")

    print("\n全部通过。")
    return 0


if __name__ == "__main__":
    import sys
    raise SystemExit(_selftest(live="--live" in sys.argv))
