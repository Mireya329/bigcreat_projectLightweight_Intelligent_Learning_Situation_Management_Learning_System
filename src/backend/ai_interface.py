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
import os
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

# 考研院校库是否已接入（4 号提供）。False 时 school_recommend 直接返回空，
# 不让模型编校名——实测 0.5b 会把输入里的"某二本"抄成推荐院校。
SCHOOL_DB_READY = False

# 队长的模型提供方（环境变量 AI_PROVIDER 可覆盖，例如 set AI_PROVIDER=gateway）
#   "local"   直连本机 Ollama。重试由本模块负责（见 MAX_RETRY）
#   "gateway" 队长 AI 网关。网关内部已做提示词、JSON 约束、输出校验和一次重试，
#             因此本模块**不再叠加同类重试**（队长 2026-09-27 回复第四节第 2 条）
AI_PROVIDER = os.environ.get("AI_PROVIDER", "local").strip().lower() or "local"

# 队长网关地址（2026-09-27 回复：POST http://127.0.0.1:8765/ai）
# ⚠️ 127.0.0.1 指**调用者所在电脑**：联调时要在本机同时跑 Ollama 和队长网关，
#    当前服务未开放局域网，不能拿这个地址跨电脑访问。
AI_GATEWAY_URL = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8765/ai").strip()

# 队长网关：单次模型调用超时 180 秒、最多两次输出尝试 → 后端等候设 370 秒
GATEWAY_TIMEOUT = int(os.environ.get("AI_GATEWAY_TIMEOUT", "370"))

# 本轮联调是否允许 AI 归因自动成为正式分类。
# 队长 2026-09-27 回复第三节：本轮**所有 AI 归因交人工复核**，
# 待人工题集质量验收通过后才开放自动入库；开放后仍执行 <0.5 必须复核。
AUTO_COMMIT_CLASSIFICATION = False


# ============================================================
# 协议常量
# ============================================================
# 2026-10-09 起用队长交付包的 v2 正式版本，不再用 2026-09-21-captain-v1
# 去描述已经改变的行为（队长《01_第二轮回复与字段变更》第一节）。
#
# 权威来源是包里的 protocol.json / schemas.json；包不在时退回下面的常量。
# ⚠️ 反过来不成立：网关发回来的 protocol 对象**原样透传**，
#    不许改写成旧版本或临时后缀版本（队长原话："不重新写成旧版本"）。
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))          # src/backend
_PROJECT_ROOT = os.path.dirname(os.path.dirname(_BACKEND_DIR))     # 项目根
CAPTAIN_PKG_DIR = os.path.join(_PROJECT_ROOT, "vendor", "captain_ai_v2")


def _load_pkg_json(name: str, default):
    """读交付包里的 JSON。读不到就退回默认值，不让异常打断调用链。"""
    try:
        with open(os.path.join(CAPTAIN_PKG_DIR, name), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


PROTOCOL_JSON = _load_pkg_json("protocol.json", {})
SCHEMAS_JSON = _load_pkg_json("schemas.json", {})

PROTOCOL_VERSION = str(PROTOCOL_JSON.get("version")
                       or "2026-10-09-captain-gateway-v2")
SCHEMA_VERSION = str(PROTOCOL_JSON.get("schema_version")
                     or "2026-10-09-captain-schema-v2")
MAX_OUTPUT_RETRIES = int(PROTOCOL_JSON.get("max_output_retries", 1) or 1)

# ⚠️ code=2 语义分裂（队长 2026-09-27 第四节第 2 条 + 2026-10-09 v2 确认）
# 网关分支：  code=2 = 请求字段错误。修正字段后发的是**新请求**，
#             不能让程序原样重复错误请求 → 不自动重试。
# 本地分支：  没有网关替我重试，保留原有的"输出不合法重试 1 次"。
CODE_SEMANTICS = {
    # 直连 Ollama 分支
    "local": {
        "version": PROTOCOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "0": "成功",
        "1": "模型异常 / 重试后仍降级",
        "2": "模型输出格式不合法（后端重试 %d 次）" % MAX_RETRY,
        "code2": "模型输出格式不合法（后端重试 %d 次）" % MAX_RETRY,
        "retry_owner": "backend",
        "max_output_retries": MAX_RETRY,
    },
    # 网关分支：字段与包内 protocol.json 一致
    "gateway": {
        "version": PROTOCOL_VERSION,
        "schema_version": SCHEMA_VERSION,
        "0": "成功",
        "1": "模型侧最终失败（网关已内部重试）",
        "2": "请求字段错误；修改请求后才能重新提交，不自动重试",
        "code2": "请求字段错误；修改请求后才能重新提交，不自动重试",
        "retry_owner": "gateway",
        "max_output_retries": MAX_OUTPUT_RETRIES,
    },
}


def code_semantics() -> dict:
    """当前生效的 code 语义（随 AI_PROVIDER 变化）。"""
    return CODE_SEMANTICS.get(AI_PROVIDER, CODE_SEMANTICS["local"])

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

# ⚠️ label 必须以队长交付包 schemas.json 的 enum 为准，不要照自己的习惯写。
#    我 v1 时期写成"概念理解错误"，与协议枚举"概念不清"不符——
#    2026-10-11 真机时还据此把 7B 的正确输出判成了不合格（见错误日志条目 37）。
#    现改为从 vendor/captain_ai_v2/schemas.json 直接读取，包不在才用下面兜底。
ERROR_TYPE_CODES: dict[str, str] = {    # 四个 code，队长协议冻结，不可自行增删
    "concept_misunderstanding": "概念不清",
    "calculation_error": "计算失误",
    "misread_question": "审题偏差",
    "method_gap": "方法缺失",
}


def _labels_from_schema() -> dict[str, str]:
    """从交付包 schemas.json 读 label 枚举，覆盖上面的兜底表。

    schemas 里 error_type.enum 与 error_type_label.enum 是同序的两列，
    按位配对即可。读不到任何一步就返回空 dict，让兜底表继续生效。
    """
    try:
        props = SCHEMAS_JSON["error_analysis"]["properties"]
        codes = props["error_type"]["enum"]
        labels = props["error_type_label"]["enum"]
        if len(codes) != len(labels):
            return {}
        return {str(c): str(l) for c, l in zip(codes, labels)}
    except (KeyError, TypeError, AttributeError):
        return {}


ERROR_TYPE_CODES.update(_labels_from_schema())

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
    # 网关发回来的 protocol 对象，原样透传。
    # 队长 v2（2026-10-09）："后端保留网关发来的版本，不重新写成旧版本
    # 或临时后缀版本"。没有网关 protocol 时（本地分支）才用我侧的语义表。
    protocol: Optional[dict] = None

    def to_envelope(self) -> dict:
        """队长 v2 返回体：{code, scene, data, raw, model, elapsed_ms, protocol}"""
        env = {
            "code": self.code,
            "scene": self.scene,
            "data": self.data,
            "raw": self.raw if self.raw is not None else {},
            "model": self.model,
            "elapsed_ms": self.elapsed_ms,
        }
        if isinstance(self.protocol, dict) and self.protocol:
            env["protocol"] = self.protocol          # 网关原样，绝不改写
        else:
            sem = code_semantics()
            env["protocol"] = {
                "version": sem["version"],
                "schema_version": sem["schema_version"],
                "code2": sem["code2"],
                "retry_owner": sem["retry_owner"],
                "max_output_retries": sem["max_output_retries"],
            }
        return env

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


# 主场景（按 scene 查）
# 2026-10-09 起字段以队长交付包 schemas.json 为准（不再是我侧提案）：
#   error_analysis  新增 answer、suggestions；solution 改数组；去掉 similar_question
#   weak_diagnosis  advice → suggestions；新增 summary
#   essay_review    corrected → revised_essay；issues → corrections(对象数组)
SCENE_SPECS: dict[str, SceneSpec] = {
    "error_analysis": SceneSpec(
        required=("answer", "solution", "knowledge_points", "error_type",
                  "error_type_label", "error_confidence", "error_reason",
                  "suggestions"),
    ),
    "qa": SceneSpec(
        required=("answer", "steps", "knowledge_points"),
    ),
    "weak_diagnosis": SceneSpec(
        required=("summary", "weak_points", "suggestions"),
    ),
    # 择校：队长 v2 明确"当前只返回失败信封"，所以三档不再必填，
    # code=1 时也不去检查它们（见 validate()）
    "school_recommend": SceneSpec(
        required=(),
        optional=("reach", "match", "safety", "note"),
    ),
    "essay_review": SceneSpec(
        required=("overall_feedback", "corrections", "revised_essay",
                  "suggestions"),
    ),
    "judge_fragment": SceneSpec(     # 本侧扩展，队长同意保持独立、不进主协议
        required=("choice",),
        optional=("guessed_no", "stem", "qtype", "reason"),
    ),
}

# qa 的六个功能里，三个走 scene=qa 但数据形态完全不同
# （队长 2026-09-27 回复：数学指导 math_guidance / 政治抽查 politics_quiz /
#   复习规划 study_plan 都用 scene=qa，靠 user_context.task 区分）
#
# 2026-10-09：已按队长 schemas.json 对齐，不再是提案。
#   politics_quiz 不再要 verdict；参考答案用 reference_answer；
#   study_plan 要 summary + plan（项为 day/task/minutes/success_check）
QA_TASK_SPECS: dict[str, SceneSpec] = {
    "math_guidance": SceneSpec(
        required=("answer", "steps", "knowledge_points"),
    ),
    "politics_quiz": SceneSpec(          # 政治抽查：输入侧用 student_answer
        required=("question", "reference_answer", "key_points", "feedback"),
    ),
    "study_plan": SceneSpec(
        required=("summary", "plan"),
    ),
}

# 六项功能 → (scene, task)。队长 2026-09-27 回复第二节第 4 条。
FEATURE_MAP: dict[str, tuple[str, Optional[str]]] = {
    "错题解析": ("error_analysis", None),
    "数学指导": ("qa", "math_guidance"),
    "政治抽查": ("qa", "politics_quiz"),
    "作文润色": ("essay_review", None),
    "薄弱诊断": ("weak_diagnosis", None),
    "复习规划": ("qa", "study_plan"),
}

# user_context 补充字段的取值约束（队长给定）
PLAN_DAYS_RANGE = (1, 7)                # 计划天数，默认 3
PLAN_DAYS_DEFAULT = 3
MINUTES_PER_DAY_RANGE = (1, 480)        # 每天可用分钟，默认 60
MINUTES_PER_DAY_DEFAULT = 60


def spec_for(scene: str, task: Optional[str] = None) -> SceneSpec:
    """取当前场景（+ 子任务）的字段契约。

    qa 场景必须按 task 分派，否则不同功能会互相串字段——
    队长明确要求"不能只按 scene 统一要求 answer 字段"。
    """
    if scene == "qa" and task:
        return QA_TASK_SPECS.get(task, SCENE_SPECS["qa"])
    return SCENE_SPECS.get(scene, SceneSpec(required=()))


# 上一次 normalize_user_context 的调整记录（给日志/排障用，不进请求体）
LAST_USER_CONTEXT_NOTES: dict = {}


def _coerce_plan_int(value: Any, lo: int, hi: int, default: int,
                     name: str, notes: dict) -> int:
    """规划参数的取值规则——队长 v2 第二节，后端与网关必须用同一套：

        None / 缺失          → 用默认值
        明确整数             → 夹取到 [lo, hi]
        整数形式的字符串     → 转换后夹取（"60" 可以）
        布尔 / 小数 / 数组 / 无效字符串 → **拒绝该值**，改用默认

    "类型错误拒绝"而不是"夹取"，是为了不把 true、60.5 之类悄悄变成 60 或 480；
    被拒绝和被夹取都记进 notes，保证**生效值可见**。
    """
    if value is None:
        return default

    def reject(why):
        notes[name] = {"给定值": value, "生效值": default, "原因": why}
        return default

    if isinstance(value, bool):
        return reject("布尔值不是有效数字")
    if isinstance(value, int):
        v = value
    elif isinstance(value, float):
        return reject("小数不是有效整数")
    elif isinstance(value, str):
        s = value.strip()
        if re.fullmatch(r"[+-]?\d+", s):
            v = int(s)
        else:
            return reject("字符串不是整数形式")
    else:
        return reject(f"类型 {type(value).__name__} 不是有效数字")

    clamped = min(max(v, lo), hi)
    if clamped != v:
        notes[name] = {"给定值": v, "生效值": clamped, "原因": "超出范围已夹取"}
    return clamped


def normalize_user_context(uc: Optional[dict]) -> dict:
    """规整 user_context：补默认值、夹取越界值、拒绝类型错误的值。

    队长 v2 给定范围：plan_days 1~7（默认 3）、available_minutes_per_day
    1~480（默认 60）。生效值写回 uc（可见），调整详情记进
    LAST_USER_CONTEXT_NOTES 并打印一行，不做静默变更。
    """
    global LAST_USER_CONTEXT_NOTES
    notes: dict = {}
    uc = dict(uc or {})
    if "task" in uc and uc["task"] not in QA_TASK_SPECS:
        # 未知 task 就当没有，走 qa 默认契约；不静默丢弃，记进 notes
        notes["task"] = {"给定值": uc["task"], "生效值": None,
                         "原因": "未知 task，按 qa 默认契约处理"}
        uc["_unknown_task"] = uc["task"]
        uc["task"] = None

    uc["plan_days"] = _coerce_plan_int(
        uc.get("plan_days"), PLAN_DAYS_RANGE[0], PLAN_DAYS_RANGE[1],
        PLAN_DAYS_DEFAULT, "plan_days", notes)
    uc["available_minutes_per_day"] = _coerce_plan_int(
        uc.get("available_minutes_per_day"), MINUTES_PER_DAY_RANGE[0],
        MINUTES_PER_DAY_RANGE[1], MINUTES_PER_DAY_DEFAULT,
        "available_minutes_per_day", notes)

    LAST_USER_CONTEXT_NOTES = notes
    if notes:
        print(f"[ai_interface] user_context 已调整：{json.dumps(notes, ensure_ascii=False)}")
    return uc


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
    # qa 的三个子功能（靠 user_context.task 分派，数据形态各不相同）
    "qa_math_guidance": (
        "你是数学辅导老师。请给出该题的最终答案与分步骤推导，并指出易错点。\n"
        "学科：{subject_cn}\n题目：{question}"
    ),
    "qa_politics_quiz": (
        "你是政治课抽查老师。请判定学生的作答是否到位：给出判定、参考答案、\n"
        "应踩到的分点、学生漏掉的分点，并给一句话点评。\n"
        "学科：{subject_cn}\n题目：{question}\n学生作答：{student_answer}"
    ),
    "qa_study_plan": (
        "你是复习规划助手。请根据可用时间和计划天数，给出按天的复习安排。\n"
        "只做计划，不要编造学生没提供过的成绩或排名。\n"
        "学科：{subject_cn}\n需求：{question}\n"
        "每天可用分钟：{available_minutes_per_day}\n计划天数：{plan_days}"
    ),
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
QA_TASK_FIELD_HINTS: dict[str, str] = {
    "math_guidance": (
        '- "answer": 字符串，最终答案\n'
        '- "steps": 字符串数组，分步骤的推导过程（1~8 条）\n'
        '- "knowledge_points": 字符串数组，关联知识点（1~8 个）\n'
        '只输出这 3 个字段'
    ),
    "politics_quiz": (
        '- "question": 字符串，题目\n'
        '- "reference_answer": 字符串，参考答案\n'
        '- "key_points": 字符串数组，应踩到的分点（1~8 个）\n'
        '- "feedback": 字符串，针对学生作答的反馈（未作答要说明"尚未作答"）\n'
        '只输出这 4 个字段'
    ),
    "study_plan": (
        '- "summary": 字符串，一句话说明这份计划的安排思路\n'
        '- "plan": 对象数组，每天一项，含 day（整数 1~7）、task（任务）、'
        'minutes（分钟 1~480）、success_check（完成标准）\n'
        '只输出这 2 个字段，天数必须等于要求的天数'
    ),
}


def field_hints_for(scene: str, task: Optional[str] = None) -> str:
    """取该场景（+ 子任务）的字段说明，拼进 Prompt 的输出格式约束里。"""
    if scene == "qa" and task:
        return QA_TASK_FIELD_HINTS.get(task, FIELD_HINTS["qa"])
    return FIELD_HINTS.get(scene, "")


# 2026-10-09：字段说明按队长 schemas.json 重写，模型才会按要求输出
# （additionalProperties=false，多给字段一律判不合法）
FIELD_HINTS: dict[str, str] = {
    "error_analysis": (
        '- "answer": 字符串，正确答案\n'
        '- "solution": 字符串数组，解题思路（1~8 条，分步骤写）\n'
        '- "knowledge_points": 字符串数组，关联知识点（1~8 个）\n'
        '- "error_type": 只能是 concept_misunderstanding / calculation_error / '
        'misread_question / method_gap 之一\n'
        '- "error_type_label": 与上面 code 对应的中文标签之一：'
        '概念不清 / 计算失误 / 审题偏差 / 方法缺失\n'
        '- "error_confidence": 0 到 1 之间的小数，表示你对这个判定的把握\n'
        '- "error_reason": 一句话说明判定依据\n'
        '- "suggestions": 字符串数组，改进建议（1~8 条）\n'
        '只输出这 8 个字段，不要多加任何字段'
    ),
    "qa": (
        '- "answer": 字符串，回答正文\n'
        '- "steps": 字符串数组，推导或讲解步骤（1~8 条）\n'
        '- "knowledge_points": 字符串数组，关联知识点（1~8 个）\n'
        '只输出这 3 个字段'
    ),
    "weak_diagnosis": (
        '- "summary": 字符串，一句话诊断结论\n'
        '- "weak_points": 对象数组，每项含 name（薄弱点名称）、'
        'evidence（依据）、advice（建议）\n'
        '- "suggestions": 字符串数组，复习建议（1~8 条）\n'
        '只输出这 3 个字段'
    ),
    "school_recommend": (
        '- "reach": 字符串数组，冲档院校\n'
        '- "match": 字符串数组，稳档院校\n'
        '- "safety": 字符串数组，保档院校\n'
        '- "note": 字符串，说明（可选）'
    ),
    "essay_review": (
        '- "overall_feedback": 字符串，总体评价\n'
        '- "corrections": 对象数组，每项含 original（原文）、'
        'corrected（改后）、reason（原因）\n'
        '- "revised_essay": 字符串，润色后的全文\n'
        '- "suggestions": 字符串数组，改进建议（1~8 条）\n'
        '只输出这 4 个字段'
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
        "answer": "[占位] 答案",
        "solution": ["[占位] 第 2 阶段接入 Ollama 后返回真实解析。"],
        "error_type": "method_gap",
        "error_type_label": "方法缺失",
        "error_confidence": 0.0,
        "error_reason": "占位数据，非真实判定",
        "knowledge_points": [],
        "suggestions": ["[占位] 建议"],
    },
    "qa": {"answer": "[占位] 知识点问答结果。", "steps": ["[占位] 步骤"],
           "knowledge_points": []},
    "weak_diagnosis": {"summary": "[占位] 薄弱点诊断结果。",
                       "weak_points": [], "suggestions": ["[占位] 建议"]},
    "school_recommend": {"reach": [], "match": [], "safety": [], "note": "[占位] 院校库未接入。"},
    "essay_review": {"overall_feedback": "[占位] 总体评价。",
                     "corrections": [], "revised_essay": "[占位] 作文批改结果。",
                     "suggestions": ["[占位] 建议"]},
    "judge_fragment": {"choice": "C", "reason": "占位数据"},
}

# 旧调用方可能直接读 MOCK_OUTPUTS，保留一个兼容视图
# qa 三个子功能的占位数据（离线/MOCK 时用，字段与 QA_TASK_SPECS 对齐）
QA_MOCK_DATA: dict[str, dict] = {
    "math_guidance": {"answer": "[占位] 数学指导", "steps": ["[占位] 步骤"],
                      "knowledge_points": []},
    "politics_quiz": {"question": "[占位] 题目",
                      "reference_answer": "[占位] 参考答案",
                      "key_points": ["[占位] 踩分点"],
                      "feedback": "[占位] 反馈"},
    "study_plan": {"summary": "[占位] 计划说明",
                   "plan": [{"day": 1, "task": "[占位] 第 1 天安排",
                             "minutes": 60, "success_check": "[占位] 完成标准"}]},
}

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
                  user_context: dict, task: Optional[str] = None) -> dict:
    """队长 AI 网关分支：POST {scene, subject, input, user_context}。

    返回网关的**完整信封**（含 code / data / raw），不在这里拆 data——
    因为网关的 code=2 表示"请求字段错误"，拆掉就丢失了这个信息。

    ⚠️ 网关内部已完成提示词、JSON 约束、输出校验和一次重试，
       所以本模块**不再叠加同类重试**（队长 2026-09-27 回复第四节第 2 条）。
    """
    body: dict[str, Any] = {
        "scene": scene,
        "subject": subject,
        "input": input_,
        "user_context": user_context or {},
    }
    req = urllib.request.Request(
        AI_GATEWAY_URL, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=GATEWAY_TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8"))


def check_ollama_alive() -> bool:
    """部署检查：探测模型服务是否在线（对接组长时先用这个）。"""
    try:
        if AI_PROVIDER == "gateway":
            if not AI_GATEWAY_URL:
                return False
            # 网关根路径未必接受 GET，用 HEAD 探活，失败也不抛
            try:
                r = urllib.request.Request(AI_GATEWAY_URL, method="HEAD")
                urllib.request.urlopen(r, timeout=5)
            except Exception:
                pass
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


def schema_key_for(scene: str, task: Optional[str] = None) -> str:
    """scene(+task) → 队长 schemas.json 里的键名。

    qa 的三个子任务里，math_guidance 与不传 task 时都用 "qa"；
    politics_quiz / study_plan 在 schemas.json 里是独立键。
    """
    if scene == "qa":
        return task if task in ("politics_quiz", "study_plan") else "qa"
    return scene


def _type_ok(value: Any, want: str) -> bool:
    if want == "object":
        return isinstance(value, dict)
    if want == "array":
        return isinstance(value, list)
    if want == "string":
        return isinstance(value, str)
    if want == "integer":
        # 布尔值在 Python 里是 int 的子类，但协议里不该当成数字
        return isinstance(value, int) and not isinstance(value, bool)
    if want == "number":
        return (isinstance(value, (int, float))
                and not isinstance(value, bool))
    return True


def _validate_schema(data: Any, schema: dict, path: str = "") -> tuple[bool, str]:
    """轻量 JSON Schema 校验（够用即可，不引第三方依赖）。

    支持队长 schemas.json 用到的全部关键字：type / required / properties /
    additionalProperties / enum / minimum / maximum / minLength / minItems /
    maxItems / items（可嵌套对象）。
    """
    want = schema.get("type")
    if want and not _type_ok(data, want):
        return False, f"{path or 'data'} 类型应为 {want}，实际是 {type(data).__name__}"

    if want == "object" or (want is None and isinstance(data, dict)):
        if not isinstance(data, dict):
            return True, ""
        for key in schema.get("required", []):
            if key not in data:
                return False, f"{path or 'data'} 缺少必填字段: {key}"
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extra = [k for k in data if k not in props]
            if extra:
                return False, f"{path or 'data'} 出现未定义字段: {extra}"
        for key, sub in props.items():
            if key in data:
                ok, why = _validate_schema(data[key], sub,
                                           f"{path}.{key}" if path else key)
                if not ok:
                    return False, why

    if isinstance(data, list):
        if "minItems" in schema and len(data) < schema["minItems"]:
            return False, f"{path or 'data'} 至少 {schema['minItems']} 项，实际 {len(data)}"
        if "maxItems" in schema and len(data) > schema["maxItems"]:
            return False, f"{path or 'data'} 最多 {schema['maxItems']} 项，实际 {len(data)}"
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(data):
                ok, why = _validate_schema(item, item_schema,
                                           f"{path}[{i}]" if path else f"[{i}]")
                if not ok:
                    return False, why

    if isinstance(data, str):
        if "minLength" in schema and len(data.strip()) < schema["minLength"]:
            return False, f"{path or 'data'} 不能为空字符串"
        if "enum" in schema and data not in schema["enum"]:
            return False, f"{path or 'data'} 取值不在枚举内: {data!r}"

    if isinstance(data, (int, float)) and not isinstance(data, bool):
        if "minimum" in schema and data < schema["minimum"]:
            return False, f"{path or 'data'} 小于下限 {schema['minimum']}: {data}"
        if "maximum" in schema and data > schema["maximum"]:
            return False, f"{path or 'data'} 超过上限 {schema['maximum']}: {data}"

    if "enum" in schema and want != "string" and data not in schema["enum"]:
        return False, f"{path or 'data'} 取值不在枚举内: {data!r}"

    return True, ""


def validate(scene: str, data: Any, task: Optional[str] = None) -> tuple[bool, str]:
    """校验 data 是否满足场景契约。返回 (是否通过, 不通过的原因)。

    task 只在 scene=qa 时起作用——三个 qa 子功能的数据形态不同，
    按同一套字段校验会把复习规划误判成缺 answer。
    """
    if scene not in SCENE_SPECS:
        return False, f"未知 scene: {scene}"

    if not isinstance(data, dict):
        return False, "data 不是 JSON 对象"

    # 优先用队长 schemas.json 做结构与类型校验（含 additionalProperties=false）
    schema = SCHEMAS_JSON.get(schema_key_for(scene, task))
    if schema:
        ok, why = _validate_schema(data, schema)
        if not ok:
            return False, why
    else:
        # 包里没有 schema 的场景（judge_fragment / school_recommend）退回必填校验
        spec = spec_for(scene, task)
        missing = [k for k in spec.required
                   if k not in data or data[k] in (None, "", [])]
        if missing:
            return False, f"缺少或空字段: {missing}"

    if scene == "error_analysis":
        et = str(data.get("error_type", "")).strip().lower()
        if et not in ERROR_TYPE_CODES:
            return False, f"error_type 不在四个 code 内: {data.get('error_type')!r}"
        # label 不在这里判死——只记录，交给 normalize_error_analysis 以 code 覆盖。
        #
        # 改这个的原因（2026-10-11 真机）：7B 把 concept_misunderstanding 的
        # label 写成"概念不清"（应为"概念理解错误"），其余字段全对、无作答时
        # 置信度也正确归 0，却因为我侧这一条被判 code=1 降级，整条结果丢掉。
        # label 是给人看的展示值，code 才是入库依据；为了同义差异丢弃一次
        # 正确调用不划算。网关那边仍会照自己的规则重试，两边口径不冲突。
        # 一致性由 normalize_error_analysis 兜底（见 test 第 23 组）。
        try:
            conf = float(data.get("error_confidence"))
        except (TypeError, ValueError):
            return False, f"error_confidence 不是数值: {data.get('error_confidence')!r}"
        if not 0.0 <= conf <= 1.0:
            return False, f"error_confidence 超出 0~1: {conf}"
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
        # 判 B = "这是一道被漏掉的独立题" → ocr_recover 会 INSERT 一道新题。
        # 补一道不存在的题比漏一道更糟，所以 B 是三个选项里唯一会写库的，
        # 必须要求模型同时给出题干，否则按不合法处理（不能只靠模型自觉）。
        # 实测（2026-10-11，7B）模型确实会填 stem，但那是模型表现不是契约保证。
        if ch == "B" and not str(data.get("stem", "")).strip():
            return False, "choice=B 必须同时给出 stem（题干），否则无法安全补题"

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


def render_content(scene: str, data: dict, task: Optional[str] = None) -> str:
    """把结构化 data 渲染成人能读的文本。

    入库的是这个渲染结果，不是模型原文——这是队长"不得原文入库"的落点。
    qa 的三个子功能渲染成完全不同的东西，所以必须带 task。
    """
    if scene == "error_analysis":
        kp = data.get("knowledge_points") or []
        kp_s = "、".join(str(x) for x in kp) if kp else "（未给出）"
        sol = data.get("solution")          # v2：字符串数组
        sol_s = ("\n".join(f"  {n}. {x}" for n, x in enumerate(sol, 1))
                 if isinstance(sol, list) else str(sol or "（未给出）"))
        sug = data.get("suggestions") or []
        sug_s = "\n".join(f"  - {x}" for x in sug) if sug else "  （未给出）"
        return (
            f"【正确答案】{data.get('answer', '（未给出）')}\n\n"
            f"【解题思路】\n{sol_s}\n\n"
            f"【错误原因】{data.get('error_type_label', '')}"
            f"（{data.get('error_type', '')}，置信度 {data.get('error_confidence', 0)}）\n"
            f"判定依据：{data.get('error_reason', '')}\n\n"
            f"【关联知识点】{kp_s}\n\n"
            f"【改进建议】\n{sug_s}"
        )
    if scene == "qa" and task:
        return _render_qa_task(task, data)
    if scene == "qa":
        return str(data.get("answer", ""))
    if scene == "weak_diagnosis":
        wp = data.get("weak_points") or []
        if wp and isinstance(wp[0], dict):          # v2：name / evidence / advice
            wp_s = "\n".join(
                f"  - {x.get('name', '')}：{x.get('evidence', '')}\n"
                f"    建议：{x.get('advice', '')}" for x in wp)
        else:
            wp_s = "\n".join(f"  - {x}" for x in wp)
        sug = data.get("suggestions") or []
        sug_s = "\n".join(f"  - {x}" for x in sug) if sug else "  （未给出）"
        return (f"【诊断结论】{data.get('summary', '')}\n\n"
                f"【薄弱点】\n{wp_s or '  （未给出）'}\n\n"
                f"【复习建议】\n{sug_s}")
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
        corr = data.get("corrections") or []
        if corr and isinstance(corr[0], dict):      # v2：original/corrected/reason
            c_s = "\n".join(
                f"  {n}. {x.get('original', '')} → {x.get('corrected', '')}\n"
                f"     原因：{x.get('reason', '')}" for n, x in enumerate(corr, 1))
        else:
            c_s = "\n".join(f"  {n}. {x}" for n, x in enumerate(corr, 1))
        sug = data.get("suggestions") or []
        sug_s = "\n".join(f"  - {x}" for x in sug) if sug else "  （未给出）"
        return (f"【总体评价】{data.get('overall_feedback', '')}\n\n"
                f"【修改明细】\n{c_s or '  （无）'}\n\n"
                f"【润色后全文】\n{data.get('revised_essay', '')}\n\n"
                f"【改进建议】\n{sug_s}")
    if scene == "judge_fragment":
        ch = str(data.get("choice", "")).strip().upper()[:1]
        extra = ""
        if ch == "B":
            extra = (f"\n题号猜测：{data.get('guessed_no', '未知')}"
                     f"\n题干：{data.get('stem', '')}"
                     f"\n题型：{data.get('qtype', '未知')}")
        return f"{ch}{extra}\n理由：{data.get('reason', '')}"
    return json.dumps(data, ensure_ascii=False, indent=2)


def _render_qa_task(task: str, data: dict) -> str:
    """qa 三个子功能的渲染——形态完全不同，不能共用一套。"""
    if task == "math_guidance":
        steps = data.get("steps") or []
        st_s = "\n".join(f"  {n}. {x}" for n, x in enumerate(steps, 1)) or "  （未给出）"
        kp = data.get("knowledge_points") or []
        out = f"【解答】{data.get('answer', '')}\n\n【步骤】\n{st_s}"
        if kp:
            out += f"\n\n【关联知识点】{'、'.join(str(x) for x in kp)}"
        return out

    if task == "politics_quiz":
        kp = data.get("key_points") or []
        kp_s = "\n".join(f"  - {x}" for x in kp) if kp else "  （未给出）"
        # 队长要求：参考答案默认折叠、未作答显示"尚未作答"
        # 渲染层只标注，是否折叠由前端决定
        return (f"【题目】{data.get('question', '')}\n\n"
                f"【参考答案】（默认折叠）{data.get('reference_answer', '')}\n\n"
                f"【踩分点】\n{kp_s}\n\n"
                f"【反馈】{data.get('feedback', '')}")

    if task == "study_plan":
        plan = data.get("plan") or []
        if plan and isinstance(plan[0], dict):      # v2：day/task/minutes/success_check
            pl_s = "\n".join(
                f"  第 {x.get('day', '?')} 天（{x.get('minutes', '?')} 分钟）："
                f"{x.get('task', '')}\n    完成标准：{x.get('success_check', '')}"
                for x in plan)
        else:
            pl_s = "\n".join(f"  - {x}" for x in plan) or "  （未给出）"
        return (f"【计划说明】{data.get('summary', '')}\n\n"
                f"【按天安排】\n{pl_s}")

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

    两条通路，行为不同（队长 2026-09-27 回复第四节第 2 条）：

    local（直连 Ollama）
        拼 Prompt → 调模型 → 解析 JSON → 校验
        → 不过则**后端重试 1 次** → 仍不过则降级（code=1，data=None，保留 raw）

    gateway（队长网关）
        网关内部已完成提示词、JSON 约束、输出校验和一次重试，
        所以本模块**只发一次请求**，不叠加同类重试。且网关的 code=2 表示
        "请求字段错误"（不是"模型输出不合法"），重试没有意义，直接反馈调用方。
        本侧仍继续做字段校验，但校验不过就降级，不再回头重试模型。
    """
    if req.scene not in SCENE_ALIASES:
        raise ValueError(f"未知 scene: {req.scene}，可选: {sorted(SCENE_ALIASES)}")

    scene, variant = SCENE_ALIASES[req.scene]
    if req.variant:
        variant = req.variant

    uc = normalize_user_context(req.user_context)
    task = uc.get("task") if scene == "qa" else None
    if task:
        variant = task                     # 借 variant 通道选 qa 子模板
    tpl_key = f"{scene}_{variant}" if variant else scene
    if tpl_key not in PROMPT_TEMPLATES:
        tpl_key = scene

    t0 = time.time()

    # ---- 请求字段自检：两处 student_answer 内容不同 → code=2 ----
    # 队长 v2 第一节："政治抽查已支持 input.student_answer，也继续兼容
    # user_context.student_answer；两处不同内容返回 code=2，避免静默覆盖。"
    # 这条在本地分支也拦——与其让模型拿到一个说不清用哪个的作答，
    # 不如当场告诉调用方"你的请求自相矛盾"。
    conflict = _student_answer_conflict(req.input, req.user_context)
    if conflict is not None:
        return AIResponse(
            ok=False, scene=scene, content="",
            raw={"reason": "conflicting_student_answer",
                 "input_student_answer": conflict[0],
                 "user_context_student_answer": conflict[1]},
            elapsed_ms=_elapsed(t0), code=2, data=None,
            model=MODEL_NAME,
            error=("请求字段错误（code=2）：input.student_answer 与 "
                   "user_context.student_answer 内容不一致，请只保留一处"),
            protocol=_gateway_protocol(None) if AI_PROVIDER == "gateway" else None,
        )

    # ---- 网关前置自检：本侧字段不合规时，别浪费一次 HTTP 往返 ----
    # 网关只认 5 个 scene（judge_fragment 是后端扩展，网关明确不支持），
    # input 的 key 也只允许 6 个。这两条不过，网关必然回 code=2 且重试无意义。
    if AI_PROVIDER == "gateway":
        why = _gateway_request_error(scene, req.input)
        if why:
            return AIResponse(
                ok=False, scene=scene, content="",
                raw={"reason": "invalid_request", "detail": why,
                     "checked_by": "backend_preflight"},
                elapsed_ms=_elapsed(t0), code=2, data=None,
                model=MODEL_NAME,
                error=f"请求字段错误（code=2，网关前置自检拦下，未发请求）：{why}",
                protocol=_gateway_protocol(None),
            )

    # ---- 分支一：MOCK 或服务离线，直接给占位结构化数据 ----
    if MOCK_MODE or not check_ollama_alive():
        if scene == "qa" and task:
            data = QA_MOCK_DATA.get(task, {"answer": "（占位）"})
        else:
            data = dict(MOCK_DATA[scene])
        return AIResponse(
            ok=True, scene=scene, content=render_content(scene, data, task),
            raw={"mock": True}, elapsed_ms=_elapsed(t0),
            code=0, data=data, model="mock",
        )

    if AI_PROVIDER == "gateway":
        return _call_via_gateway(req, scene, uc, task, t0)
    return _call_via_ollama(req, scene, tpl_key, task, t0)


def _elapsed(t0: float) -> int:
    return int((time.time() - t0) * 1000)


# 网关侧硬约束（从 vendor/captain_ai_v2/captain_ai.py 抄下来的，别凭印象改）
GATEWAY_SCENES = ("error_analysis", "qa", "weak_diagnosis",
                  "school_recommend", "essay_review")
GATEWAY_INPUT_KEYS = {"question", "stem", "text", "essay",
                      "student_answer", "reference_answer"}
GATEWAY_PRIMARY_KEYS = ("question", "stem", "text", "essay")


def _gateway_request_error(scene: str, inp: Any) -> Optional[str]:
    """按网关规则自查请求，返回不合规原因（合规返回 None）。

    宁可在本地拦下并说清原因，也不要发出去拿一个 code=2 回来——
    网关的 code=2 表示"请求字段错误"，重试无意义，白等一个超时。
    """
    if scene not in GATEWAY_SCENES:
        return (f"scene={scene} 不在网关支持的 5 个场景内"
                f"（judge_fragment 是后端扩展，网关明确不支持，请走 local 分支）")

    if not isinstance(inp, dict):
        return None                      # 字符串由调用方保证非空

    bad = set(inp) - GATEWAY_INPUT_KEYS
    if bad:
        return (f"input 含网关不认识的字段 {sorted(bad)}，"
                f"只允许 {sorted(GATEWAY_INPUT_KEYS)}")

    primary = [inp[k] for k in GATEWAY_PRIMARY_KEYS if k in inp]
    if not primary:
        return "input 缺少 question/stem/text/essay 之一（网关要求有正文文本）"
    if any(not isinstance(x, str) or not x.strip() for x in primary):
        return "input 正文必须是非空字符串"
    if len(set(primary)) != 1:
        return "question/stem/text/essay 内容不一致（填一个就够）"

    text = primary[0]
    if "reference_answer" in inp:
        text = text + "\n参考答案：" + str(inp["reference_answer"])
    if len(text) > 6000:
        return f"input 正文 {len(text)} 字符，超过网关上限 6000"
    return None


def _student_answer_conflict(input_: Any, uc: Any) -> Optional[tuple]:
    """两处 student_answer 都有值且内容不同 → 返回 (input 值, uc 值)，否则 None。

    只有一处有值时不算冲突（队长说的是"两处不同内容"，不是"两处都填了"）。
    """
    if not isinstance(input_, dict) or not isinstance(uc, dict):
        return None
    a, b = input_.get("student_answer"), uc.get("student_answer")
    if a is None or b is None:
        return None
    if str(a).strip() == str(b).strip():
        return None
    return (a, b)


def _raw_reason(raw: Any) -> str:
    """从网关的 raw 里抠出一句人话原因（网关说 raw.reason 标明原因）。"""
    if isinstance(raw, dict):
        for k in ("reason", "error", "message", "msg"):
            v = raw.get(k)
            if v:
                return str(v)
    return ""


# 学生作答"等于没有作答"的判据（队长 v2 第五节）：
# student_answer 缺失、空白或明确写着 OCR 未识别时，归因缺少证据。
_NO_ANSWER_MARKERS = ("未识别", "未作答", "无作答", "没有作答", "无法识别",
                      "ocr未", "ocr 未")


def student_answer_missing(value: Any) -> bool:
    """学生作答是否等于"没有作答"。

    判据来自队长 v2：缺失 / 空白 / 明确 OCR 未识别。
    ⚠️ 用子串匹配会误伤极少数的正常作答（答案里恰好写了"未识别"三个字），
    权衡下来宁可保守——判成"没作答"最坏是转人工复核，
    判成"有作答"最坏是把凭空的归因写进正式库。
    """
    if value is None:
        return True
    s = str(value).strip()
    if not s:
        return True
    low = s.lower()
    return any(m in low for m in _NO_ANSWER_MARKERS)


def apply_no_answer_rule(data: dict, student_answer: Any,
                         raw_out: dict) -> dict:
    """没作答 → 归因无证据：置信度强制 0，原模型分数留 raw.model_error_confidence。

    队长 v2 第五节原话：
        "student_answer 缺失、空白或明确 OCR 未识别时，error_confidence 强制为 0，
         error_reason 说明无法归因；原模型分数留在 raw.model_error_confidence，
         便于审计。"
    注意 data.error_type 仍是候选值，**不是正式分类**，下游必须走人工复核队列。
    """
    if not isinstance(data, dict) or not student_answer_missing(student_answer):
        return data
    original = data.get("error_confidence")
    if original is not None:
        raw_out["model_error_confidence"] = original
    data["error_confidence"] = 0.0
    data["error_reason"] = ("[无学生作答，无法归因，置信度强制为 0] "
                            + str(data.get("error_reason", "")).strip())
    return data


def storage_route(response: dict) -> str:
    """AI 结果的去向（与队长包内 backend_contract.storage_route 同一口径）。

        code != 0              → failure_log_only（只记失败日志）
        scene=error_analysis   → manual_review_queue（**所有** AI 归因先过人）
        其他场景                → validated_result

    队长 v2 第五节："正式库不能直接写入候选分类。"
    """
    if response.get("code") != 0:
        return "failure_log_only"
    if response.get("scene") == "error_analysis":
        return "manual_review_queue"
    return "validated_result"


def is_confirmed_classification(row: Any) -> bool:
    """这一行算不算"已确认分类"（队长包内 is_confirmed_classification 的等价实现）。

    只看两件事：review_flag 为空 **且** error_type 在四个 code 内。
    历史遗留的 error_type 哪怕非空（如 id=13），只要还挂着 pending_review
    就不算已确认——这正是队长 2026-10-09 指出的那处口径不一致的落点。
    """
    try:
        if isinstance(row, dict):
            flag, etype = row.get("review_flag"), row.get("error_type")
        else:                                    # sqlite3.Row 也支持下标取
            flag, etype = row["review_flag"], row["error_type"]
    except (KeyError, TypeError, IndexError):
        return False
    return flag in (None, "") and etype in ERROR_TYPE_CODES


def _as_raw(raw: Any) -> dict:
    """raw 恒为对象（队长 v2 第一节）。字符串/None 一律包成对象。"""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    return {"text": str(raw)[:2000]}


def _gateway_protocol(env: Any) -> dict:
    """网关分支的 protocol 对象：优先用网关发来的，绝不改写。

    网关没给（HTTP 失败时根本没有信封）才按网关语义生成一个，
    保证"HTTP 失败、请求错误和模型失败也都有该对象"。
    """
    if isinstance(env, dict) and isinstance(env.get("protocol"), dict) \
            and env["protocol"]:
        return dict(env["protocol"])
    sem = CODE_SEMANTICS["gateway"]
    return {
        "version": sem["version"],
        "schema_version": sem["schema_version"],
        "code2": sem["code2"],
        "retry_owner": sem["retry_owner"],
        "max_output_retries": sem["max_output_retries"],
    }


def _call_via_gateway(req: AIRequest, scene: str, uc: dict,
                      task: Optional[str], t0: float) -> AIResponse:
    """网关分支：只发一次，code 语义按网关口径处理。"""
    try:
        env = _call_gateway(scene, req.subject, req.input or {}, uc, task)
    except urllib.error.HTTPError as e:
        # HTTP 状态码 ≠ 信封 code，必须分开处理（队长明确要求）
        body = ""
        try:
            body = e.read().decode("utf-8", "ignore")[:500]
        except Exception:                                # noqa: BLE001
            pass
        hint = ("服务繁忙或网关内部错误，稍后再试，不无限重试"
                if e.code >= 500 else "请求被网关拒绝，检查字段与地址")
        return AIResponse(
            ok=False, scene=scene, content="",
            raw={"http_status": e.code, "body": body,
                 "reason": f"HTTP {e.code}；{hint}"},
            elapsed_ms=_elapsed(t0), code=1, data=None,
            model=MODEL_NAME, error=f"网关 HTTP {e.code}：{hint}",
            protocol=_gateway_protocol(None),
        )
    except urllib.error.URLError as e:
        return AIResponse(
            ok=False, scene=scene, content="",
            raw={"error": str(e), "reason": "gateway_unreachable"},
            elapsed_ms=_elapsed(t0), code=1, data=None, model=MODEL_NAME,
            error=(f"网关不可达：{e}。127.0.0.1 指本机，"
                   f"联调时需同时运行本地 Ollama 和队长网关（start.ps1）"),
            protocol=_gateway_protocol(None),
        )
    except Exception as e:                               # noqa: BLE001
        return AIResponse(
            ok=False, scene=scene, content="",
            raw={"error": repr(e), "reason": "gateway_call_exception"},
            elapsed_ms=_elapsed(t0), code=1, data=None, model=MODEL_NAME,
            error=f"网关调用异常: {e!r}",
            protocol=_gateway_protocol(None),
        )

    code = env.get("code")
    raw = _as_raw(env.get("raw"))
    gmodel = env.get("model") or MODEL_NAME
    gprotocol = _gateway_protocol(env)

    if code == 2:
        # 网关口径：请求字段错误。重试无意义，原样反馈调用方。
        return AIResponse(
            ok=False, scene=scene, content="", raw=raw,
            elapsed_ms=_elapsed(t0), code=2, data=None, model=gmodel,
            error=f"网关判定请求字段错误（code=2），不重试：{_raw_reason(raw)}",
            protocol=gprotocol,
        )
    if code != 0:
        # 网关已内部重试过，这里不再叠加
        return AIResponse(
            ok=False, scene=scene, content="", raw=raw,
            elapsed_ms=_elapsed(t0), code=1, data=None, model=gmodel,
            error=(f"网关返回 code={code}（模型侧已重试仍失败）："
                   f"{_raw_reason(raw)}"),
            protocol=gprotocol,
        )

    parsed = env.get("data")
    if not isinstance(parsed, dict):
        return AIResponse(
            ok=False, scene=scene, content="",
            raw={"text": json.dumps(parsed, ensure_ascii=False)[:2000],
                 "reason": "data 不是 JSON 对象"},
            elapsed_ms=_elapsed(t0), code=1, data=None, model=gmodel,
            error=f"网关 data 不是 JSON 对象：{type(parsed).__name__}",
            protocol=gprotocol,
        )

    ok, why = validate(scene, parsed, task)
    if not ok:
        # 队长：重试交给网关，后端继续校验。所以这里降级，不回头重试模型。
        return AIResponse(
            ok=False, scene=scene, content="",
            raw={"data": parsed, "reason": why, "retry_owner": "gateway"},
            elapsed_ms=_elapsed(t0), code=1, data=None, model=gmodel,
            error=f"本地字段校验未通过（网关分支不重复重试）：{why}",
            protocol=gprotocol,
        )

    if scene == "error_analysis":
        parsed = normalize_error_analysis(parsed)
        # 没作答 → 置信度强制 0（队长 v2 第五节），原分数留在 raw
        parsed = apply_no_answer_rule(
            parsed, (req.input or {}).get("student_answer"), raw)
    return AIResponse(
        ok=True, scene=scene, content=render_content(scene, parsed, task),
        raw=raw if raw else {"data": parsed},
        elapsed_ms=_elapsed(t0), code=0, data=parsed, model=gmodel, retries=0,
        protocol=gprotocol,
    )


def _call_via_ollama(req: AIRequest, scene: str, tpl_key: str,
                     task: Optional[str], t0: float) -> AIResponse:
    """直连本机 Ollama 分支：保留原有的"校验不过重试 1 次"逻辑。"""
    values = dict(req.input or {})
    values["subject_cn"] = SUBJECTS.get(req.subject, req.subject)
    uc = normalize_user_context(req.user_context)
    if uc:
        values.setdefault("available_minutes_per_day",
                          uc.get("available_minutes_per_day", ""))
        values.setdefault("plan_days", uc.get("plan_days", ""))
        values.setdefault("user_context", json.dumps(uc, ensure_ascii=False))

    prompt = _safe_format(PROMPT_TEMPLATES[tpl_key], values)
    prompt += _JSON_RULE.format(contract=field_hints_for(scene, task))

    last_raw, last_err = "", ""
    for attempt in range(MAX_RETRY + 1):
        try:
            text = _call_ollama(prompt)
        except urllib.error.URLError as e:
            # 服务连不上/超时：这属于"模型异常"，没有重试意义
            return AIResponse(
                ok=False, scene=scene, content="", raw={"error": str(e)},
                elapsed_ms=_elapsed(t0), code=1, data=None,
                model=MODEL_NAME, error=f"调用失败: {e}",
            )
        except Exception as e:                           # noqa: BLE001
            return AIResponse(
                ok=False, scene=scene, content="", raw={"error": repr(e)},
                elapsed_ms=_elapsed(t0), code=1, data=None,
                model=MODEL_NAME, error=f"调用异常: {e!r}",
            )

        parsed, last_raw = extract_json(text)
        if parsed is None:
            last_err = "返回不是合法 JSON 对象"
        else:
            ok, why = validate(scene, parsed, task)
            if ok:
                raw_obj = {"text": text}
                if scene == "error_analysis":
                    parsed = normalize_error_analysis(parsed)
                    # 没作答 → 置信度强制 0；原模型分数留 raw.model_error_confidence
                    parsed = apply_no_answer_rule(
                        parsed, values.get("student_answer"), raw_obj)
                return AIResponse(
                    ok=True, scene=scene,
                    content=render_content(scene, parsed, task),
                    raw=raw_obj, elapsed_ms=_elapsed(t0),
                    code=0, data=parsed, model=MODEL_NAME, retries=attempt,
                )
            last_err = why

    # ---- 重试耗尽 → 降级。保留 raw、置 code=1、data=None ----
    return AIResponse(
        ok=False, scene=scene, content="",
        raw={"text": last_raw, "reason": last_err, "retried": MAX_RETRY},
        elapsed_ms=_elapsed(t0),
        code=1, data=None, model=MODEL_NAME, retries=MAX_RETRY,
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

    scene = good[0].scene
    task = normalize_user_context(req.user_context).get("task") if scene == "qa" else None
    # 不同场景拿来比对的字段不同
    key_field = "choice" if scene == "judge_fragment" else "error_type"
    types = {str(r.data.get(key_field, "")) for r in good}
    stable = len(types) == 1
    elapsed = sum(r.elapsed_ms for r in results)

    base = good[0].data
    merged = dict(base)
    merged["stable"] = stable

    if scene == "judge_fragment":
        # 碎片归属判错会往库里补一道不存在的题，比不补更糟。
        # 所以不稳定时强制判 C（无法判断）→ ocr_recover 视为 unknown → 不入库。
        if stable:
            note = f"{len(good)} 轮判定一致（{types.pop()}）"
        else:
            merged["choice"] = "C"
            merged["reason"] = f"[判定未通过稳定性校验] {sorted(types)}，按无法判断处理"
            note = f"{len(good)} 轮判定不一致（{sorted(types)}），强制判 C"
    else:
        if stable:
            conf = min(float(r.data.get("error_confidence", 0) or 0)
                       for r in good)
            note = f"{len(good)} 轮分类一致（{types.pop()}），置信度取最小值 {conf}"
        else:
            conf = 0.0
            note = (f"{len(good)} 轮分类不一致（{sorted(types)}），"
                    f"置信度置 0，转人工复核")
        merged["error_confidence"] = round(conf, 3)
        if not stable:
            # 分类作废，但别给用户一个看起来像结论的东西
            merged["error_reason"] = f"[分类未通过稳定性校验] {note}"

    return AIResponse(
        ok=True, scene=good[0].scene,
        content=render_content(good[0].scene, merged, task),
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
                  stable: Optional[bool] = None,
                  reference_answer: Optional[str] = None) -> AIResponse:
    """错题智能解析（队长协议 scene=error_analysis）。

    variant="v2"          走带 OCR 残缺识别的 Prompt
    stable=True           同题多跑做分类一致性校验（默认取 STABILITY_CHECK）
    reference_answer      参考答案（网关会拼进正文，本地分支拼进 prompt）
    """
    inp = {"question": question, "student_answer": student_answer}
    if reference_answer:
        inp["reference_answer"] = reference_answer
    req = AIRequest(
        scene="error_analysis", subject=subject, variant=variant,
        input=inp,
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
                 user_context: Optional[dict] = None,
                 task: Optional[str] = None) -> AIResponse:
    """知识点智能问答（scene=qa）。

    task=None             通用答疑，data 只要 answer
    task="math_guidance"  数学指导，要 answer + steps
    task="politics_quiz"  政治抽查，输入侧用 input.student_answer，要 verdict
    task="study_plan"     复习规划，要 plan；会读 user_context 的
                          available_minutes_per_day / plan_days
    """
    return call_ai(AIRequest(scene="qa", subject=subject,
                             input={"question": question},
                             user_context=dict(user_context or {},
                                               **({"task": task} if task else {}))))


def math_guidance(question: str, subject: str = DEFAULT_SUBJECT,
                  user_context: Optional[dict] = None) -> AIResponse:
    """数学指导（六项功能之一：scene=qa + task=math_guidance）"""
    return knowledge_qa(question, subject, user_context, task="math_guidance")


def politics_quiz(question: str, student_answer: str,
                  subject: str = "politics",
                  user_context: Optional[dict] = None) -> AIResponse:
    """政治抽查（scene=qa + task=politics_quiz，学生作答走 input.student_answer）"""
    req = AIRequest(scene="qa", subject=subject,
                    input={"question": question, "student_answer": student_answer},
                    user_context=dict(user_context or {}, task="politics_quiz"))
    return call_ai(req)


def study_plan(requirement: str, plan_days: int = PLAN_DAYS_DEFAULT,
               minutes_per_day: int = MINUTES_PER_DAY_DEFAULT,
               subject: str = DEFAULT_SUBJECT,
               user_context: Optional[dict] = None) -> AIResponse:
    """复习规划（scene=qa + task=study_plan）。

    队长给定范围：plan_days 1~7、available_minutes_per_day 1~480，
    越界值由 normalize_user_context 夹取，不报错。
    """
    uc = dict(user_context or {}, task="study_plan",
              plan_days=plan_days, available_minutes_per_day=minutes_per_day)
    req = AIRequest(scene="qa", subject=subject,
                    input={"question": requirement}, user_context=uc)
    return call_ai(req)


def render_stats_text(stats: Any) -> str:
    """把学情统计写成一句句人话，再喂给模型。

    为什么要转：真机实测（2026-09-21）发现直接喂 JSON，模型会把 **字段名**
    当成薄弱点输出——`{"单词正确率": 0.72}` 让它报 `weak_points: ["单词正确率"]`，
    还有 "未分类" 这种根本不是知识点的桶名。喂自然语言后输出的是
    "单词练习错误率高、错题分布较多"，这才叫诊断。
    """
    if isinstance(stats, str):
        return stats
    if not isinstance(stats, dict):
        return str(stats)

    parts = []
    for k, v in stats.items():
        if v is None or v == "" or v == [] or v == {}:
            continue                       # 空值别进 prompt，否则模型会输出"xxx：None"
        if isinstance(v, dict):
            inner = "、".join(f"{k2}{v2}" for k2, v2 in v.items()
                              if v2 is not None and v2 != "")
            if not inner:
                continue
            parts.append(f"{k}：{inner}")
        elif isinstance(v, list):
            parts.append(f"{k}：{'、'.join(str(x) for x in v)}")
        else:
            parts.append(f"{k}：{v}")
    return "；".join(parts) + "。" if parts else "暂无学习数据。"


def diagnose_weakness(stats: dict, subject: str = DEFAULT_SUBJECT,
                      user_context: Optional[dict] = None) -> AIResponse:
    """学习薄弱点诊断（队长协议 scene=weak_diagnosis）。

    stats 可以传 dict（会转成自然语言再喂模型）或直接传 str。
    """
    return call_ai(AIRequest(
        scene="weak_diagnosis", subject=subject,
        # 字段名必须用网关白名单内的 key（stats_json 会被网关判 code=2）
        input={"text": render_stats_text(stats)},
        user_context=user_context))


def school_recommend(profile: dict, subject: str = DEFAULT_SUBJECT,
                     user_context: Optional[dict] = None) -> AIResponse:
    """择校冲稳保推荐。

    ⚠️ 院校库没接进来之前**不调模型**。
    真机实测（2026-09-21）：明知没有院校库，0.5b 仍会把输入里的
    "本科：某二本" 抄成推荐校名填进 reach/match/safety。
    让用户照着编出来的校名报志愿是要出事的，所以宁可空着。

    返回口径与队长网关一致（2026-09-27 回复第二节第 3 条）：
    缺少数据时 code=1、data=null、raw.reason=school_data_unavailable
    ——**不能把占位结果包装成成功推荐**。
    """
    if not SCHOOL_DB_READY:
        return AIResponse(
            ok=False, scene="school_recommend",
            content="【择校推荐】院校数据未就绪，暂不提供推荐。",
            raw={"reason": "school_data_unavailable"},
            elapsed_ms=0, code=1, data=None,
            model="none",
            error="院校数据未就绪（school_data_unavailable）："
                  "等 4 号提供考研院校表后再开放，避免模型编造校名",
        )
    # ① 字段名必须在网关白名单内（profile 会被判 code=2）
    # ② 内容必须是自然语言，不能喂 JSON——实测模型会把 key 抄成校名
    return call_ai(AIRequest(
        scene="school_recommend", subject=subject,
        input={"text": render_stats_text(profile)},
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

    print("\n[10] 学情转人话：别让字段名变成模型的'薄弱点'")
    # 真机实测：直接喂 JSON，模型会输出 weak_points: ["单词正确率", "未分类10"]
    txt = render_stats_text(
        {"错题数": 11, "未分类": None, "待复习": 11,
         "错误类型分布": {"未分类": 10, "计算失误": 1}})
    print(f"  {txt}")
    assert "None" not in txt, f"空值漏进了 prompt: {txt}"
    assert "错题数：11" in txt and "待复习：11" in txt
    assert "未分类：" not in txt, f"None 项应被跳过: {txt}"
    assert render_stats_text({}) == "暂无学习数据。"
    assert render_stats_text("原样字符串") == "原样字符串"
    print("  ✓ 空值已过滤；空字典给兜底文案；字符串原样返回")

    print("\n[11] 择校未接院校库 → 口径对齐队长网关：code=1、data=null")
    r = school_recommend({"本科": "某二本", "目标分": 340})
    # 队长 2026-09-27：缺少数据时明确返回"院校数据未就绪"，
    # 不能把占位结果包装成成功推荐；网关对应 code=1、data=null、
    # raw.reason=school_data_unavailable
    assert r.code == 1 and r.data is None, f"应为降级而非成功: code={r.code}"
    assert r.raw and r.raw.get("reason") == "school_data_unavailable", r.raw
    assert r.model == "none", "院校库没接入时不应调用模型"
    print(f"  ✓ code={r.code} data={r.data} raw.reason={r.raw['reason']}"
          f"（实测 0.5b 会把「某二本」抄成推荐院校）")

    print("\n[12] qa 子任务：不同 task 走不同字段契约")
    for tk, good, bad in [
        ("math_guidance", {"answer": "1", "steps": ["先求导"]}, {"answer": "1"}),
        ("politics_quiz", {"verdict": "基本正确"}, {"answer": "只是答案"}),
        ("study_plan", {"plan": ["第1天：极限"]}, {"answer": "随便"}),
    ]:
        ok_good, _ = validate("qa", good, tk)
        ok_bad, why_bad = validate("qa", bad, tk)
        assert ok_good, f"{tk} 合法数据被误判: {good}"
        assert not ok_bad, f"{tk} 缺字段却通过了: {bad}"
        print(f"  ✓ {tk:<15} 契约独立（{why_bad}）")
    # 不带 task 时仍按通用 qa 校验，向后兼容
    assert validate("qa", {"answer": "x"})[0]
    print("  ✓ 不带 task 时仍是通用 qa（answer 即可），旧调用不受影响")

    print("\n[13] user_context 规划字段夹取与未知 task 处理")
    uc = normalize_user_context({"plan_days": 99, "available_minutes_per_day": -5,
                                 "task": "not_a_task"})
    assert uc["plan_days"] == 7, uc                      # 上限夹取
    assert uc["available_minutes_per_day"] == 1, uc       # 下限夹取
    assert uc["task"] is None and uc["_unknown_task"] == "not_a_task", uc
    print(f"  ✓ 越界夹取：plan_days 99→{uc['plan_days']}、"
          f"minutes -5→{uc['available_minutes_per_day']}；未知 task 记档不静默丢弃")

    print("\n[14] 网关分支：code 语义随 AI_PROVIDER 切换")
    sem_local, sem_gw = CODE_SEMANTICS["local"], CODE_SEMANTICS["gateway"]
    assert "格式不合法" in sem_local["2"] and sem_local["retry_owner"] == "backend"
    assert "请求字段错误" in sem_gw["2"] and sem_gw["retry_owner"] == "gateway"
    assert sem_local["version"] != sem_gw["version"], "两套语义不能共用一个版本名"
    print(f"  ✓ local  code=2 =「{sem_local['2']}」")
    print(f"  ✓ gateway code=2 =「{sem_gw['2']}」")
    print(f"  ✓ 两套语义版本名不同，避免同一版本对应两套含义")

    # --- 2. 真机自测 ---
    if live:
        print("\n[15] 实时调用")
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
            # code 文案随分支变化，别把网关的 code=2 说成"格式不符"
            s = code_semantics()
            state = {0: "成功", 1: "降级/模型失败", 2: s["2"]}[env["code"]]
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
