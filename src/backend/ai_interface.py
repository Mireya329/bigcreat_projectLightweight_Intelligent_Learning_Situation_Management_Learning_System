# -*- coding: utf-8 -*-
"""
AI 接口预留层（第一阶段交付物）
=================================
项目：大学生全场景轻量化智能学情管理与学习系统
负责人：2号成员（后端 + OCR + AI 接口联动）

设计说明
--------
1. 本模块是后端与"本地 Ollama 大模型"之间唯一的通道。
   第二阶段组长完成 Ollama + 量化模型部署后，后端其他代码
   **不需要改动**，只需保证 Ollama 服务跑在本机 11434 端口。
2. 第一阶段所有功能走 MOCK_MODE（返回占位数据），先保证
   接口签名稳定、可被前端和 OCR 模块调用。
3. 第二阶段联调时：把 MOCK_MODE 改为 False 即切换真实推理。

使用方法
--------
    python ai_interface.py          # 跑内置自测（mock 模式）

记录规范
--------
每次联调报错请记入 docs/部署日志.md（命令原文 + 报错截图 + 解决办法）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from dataclasses import dataclass, field
from typing import Optional

# ============================================================
# 配置区（第 2 阶段组长部署 Ollama 后，按实际情况修改）
# ============================================================
MOCK_MODE = False                     # True=占位数据；False=真实调用 Ollama
OLLAMA_BASE_URL = "http://localhost:11434"
MODEL_NAME = "qwen2.5:0.5b"              # 组长最终选定的量化模型名，届时替换
REQUEST_TIMEOUT = 120                  # 本地 CPU 推理较慢，超时给足


# ============================================================
# 数据结构：各 AI 功能的输入输出契约（前后端共用）
# ============================================================
@dataclass
class AIRequest:
    """通用请求：scene 决定走哪个 Prompt 模板"""
    scene: str                        # explain_wrong / qa / diagnose / school_advice / essay_polish
    payload: dict = field(default_factory=dict)
    context: Optional[dict] = None    # 学情统计等附加上下文（第3阶段学情模块接入）


@dataclass
class AIResponse:
    ok: bool
    scene: str
    content: str                      # AI 输出正文
    raw: Optional[dict] = None        # 原始返回（调试用）
    elapsed_ms: int = 0


# ============================================================
# Prompt 模板（第 2 阶段组长负责调优，这里只定占位结构）
# ============================================================
PROMPT_TEMPLATES: dict[str, str] = {
    # 模块一：错题智能解析
    "explain_wrong": "你是一名大学辅导老师。以下是学生做错的一道题，"
                     "请输出：1.正确解题思路 2.错误原因归类（概念不清/计算失误/审题偏差/方法缺失）"
                     "3.关联知识点 4.一道同类变式题。\n题目：{question}\n学生答案：{student_answer}",
    # 模块二：知识点问答与背诵抽查
    "qa": "你是知识点答疑助手。用中文简洁准确地回答：{question}",
    # 模块三：薄弱点诊断（第3阶段接学情数据）
    "diagnose": "根据以下学习数据（正确率、错题分布、复习记录），"
                "诊断薄弱知识点并给出复习建议：{stats_json}",
    # 模块四：考研择校冲稳保推荐
    "school_advice": "根据学生情况（本科院校/专业/目标分数/意向地区），"
                     "按冲、稳、保三档各推荐院校并说明理由：{profile}",
    # 模块五：英语作文批改润色
    "essay_polish": "请批改以下英语作文：先指出语法错误（逐条）,"
                    "再给出润色版本，最后按四六级/考研标准打分。\n作文：{essay}",
}

# mock 占位输出（第 1 阶段联调用）
MOCK_OUTPUTS: dict[str, str] = {
    "explain_wrong": "[占位] 错题解析结果：第 2 阶段接入 Ollama 后返回真实解析。",
    "qa": "[占位] 知识点问答结果。",
    "diagnose": "[占位] 薄弱点诊断结果。",
    "school_advice": "[占位] 择校推荐结果。",
    "essay_polish": "[占位] 作文批改结果。",
}


# ============================================================
# 核心：Ollama 调用通道（预留端口，第 2 阶段激活）
# ============================================================
def _call_ollama(prompt: str) -> str:
    """通过 HTTP 调用本地 Ollama /api/chat 接口。

    ⚠️ 这是本项目唯一的 AI 出口端口。后端其他模块禁止绕过本函数
    直连模型，方便统一换模型、加缓存、记日志。
    """
    body = json.dumps({
        "model": MODEL_NAME,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }).encode("utf-8")

    req = urllib.request.Request(
        f"{OLLAMA_BASE_URL}/api/chat",
        data=body,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return data["message"]["content"]


def check_ollama_alive() -> bool:
    """部署检查：探测 Ollama 服务是否在线（对接组长时先用这个）。"""
    try:
        with urllib.request.urlopen(f"{OLLAMA_BASE_URL}/api/tags", timeout=3):
            return True
    except Exception:
        return False


# ============================================================
# 对外接口：五大 AI 功能（签名在此冻结，前端/OCR 可直接调用）
# ============================================================
def call_ai(req: AIRequest) -> AIResponse:
    """统一入口。所有功能经此调用，内部决定 mock 或真实推理。"""
    if req.scene not in PROMPT_TEMPLATES:
        raise ValueError(f"未知 scene: {req.scene}，可选: {list(PROMPT_TEMPLATES)}")

    t0 = time.time()
    if MOCK_MODE or not check_ollama_alive():
        content = MOCK_OUTPUTS[req.scene]
    else:
        prompt = PROMPT_TEMPLATES[req.scene].format(**req.payload)
        content = _call_ollama(prompt)
    return AIResponse(
        ok=True, scene=req.scene, content=content,
        elapsed_ms=int((time.time() - t0) * 1000),
    )


# ---- 便捷封装（业务侧直接调这五个函数，不用拼 scene）----

def explain_wrong_question(question: str, student_answer: str) -> AIResponse:
    """AI 错题智能解析：OCR 模块识别出题目文本后调用。"""
    return call_ai(AIRequest("explain_wrong",
                            {"question": question, "student_answer": student_answer}))


def knowledge_qa(question: str) -> AIResponse:
    """知识点智能问答 / 背诵抽查。"""
    return call_ai(AIRequest("qa", {"question": question}))


def diagnose_weakness(stats: dict) -> AIResponse:
    """学习薄弱点诊断：第 3 阶段接学情统计模块。"""
    return call_ai(AIRequest("diagnose", {"stats_json": json.dumps(stats, ensure_ascii=False)}))


def school_advice(profile: dict) -> AIResponse:
    """考研择校冲稳保推荐：第 3 阶段接考研数据库模块。"""
    return call_ai(AIRequest("school_advice", {"profile": json.dumps(profile, ensure_ascii=False)}))


def polish_essay(essay: str) -> AIResponse:
    """英语作文语法纠错与润色。"""
    return call_ai(AIRequest("essay_polish", {"essay": essay}))


# ============================================================
# OCR → AI 衔接预留（第 2 阶段打通数据链路时实现）
# ============================================================
def ocr_result_to_question(ocr_texts: list[str]) -> str:
    """把 PaddleOCR 输出的多行文本拼成完整题目字符串。

    第 2 阶段在这里做：去噪（页码/水印）、按版面坐标重排序、
    公式占位处理。当前先做最简单的按行拼接。
    """
    return "\n".join(t.strip() for t in ocr_texts if t.strip())


# ============================================================
# 自测：python ai_interface.py
# ============================================================
if __name__ == "__main__":
    print("=" * 60)
    print("AI 接口预留层 · 第一阶段自测")
    print(f"MOCK_MODE = {MOCK_MODE}")
    print(f"Ollama 在线: {check_ollama_alive()}  ({OLLAMA_BASE_URL})")
    print("=" * 60)

    tests = [
        ("错题解析", explain_wrong_question("简述哈希表的冲突解决方法", "不会")),
        ("知识点问答", knowledge_qa("什么是操作系统中的死锁？")),
        ("薄弱点诊断", diagnose_weakness({"math_correct_rate": 0.42})),
        ("择校推荐", school_advice({"target": "计算机专硕", "score": 320})),
        ("作文批改", polish_essay("I goes to school yesterday.")),
    ]
    for name, resp in tests:
        assert resp.ok, f"{name} 自测失败"
        print(f"[通过] {name:<6} {resp.content[:30]}... ({resp.elapsed_ms}ms)")

    print("\nOCR→AI 衔接测试:", ocr_result_to_question(["1. 什么是进程？", "", "2. 简述死锁条件。"])[:20], "...")
    print("\n全部通过。第 2 阶段联调时：确保 Ollama 启动，并将 MOCK_MODE 改为 False。")
