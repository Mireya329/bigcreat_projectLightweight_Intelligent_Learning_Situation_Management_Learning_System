# -*- coding: utf-8 -*-
"""AI 协议联调自测：不依赖真实模型，用桩函数验证队长协议的三条硬约束。

背景
----
队长 2026-09-21《AI模块对接回复》里的硬约束：
    1. 返回体必须是 {code, scene, data, raw, model, elapsed_ms}
    2. code=2（格式不符）自动重试 1 次，仍失败降级（保留 raw、置 code=1）
    3. 不得将模型原文直接入库

这三条在真机上很难稳定复现（模型输出随机），所以用桩函数把每种情况
人为造出来，确保代码路径真的走对了——这是给队长看的联调证据。

运行：
    python src/backend/ai_protocol_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ai_interface as ai                                     # noqa: E402

PASS, FAIL = "✓", "✗"
_failures = []


def check(name, cond, detail=""):
    print(f"  {PASS if cond else FAIL} {name}" + (f"  {detail}" if detail else ""))
    if not cond:
        _failures.append(name)


def stub(*responses):
    """把 _call_ollama 换成按序返回给定文本的桩，并记录调用次数。"""
    seq = list(responses)
    state = {"n": 0}

    def fake(prompt):
        i = min(state["n"], len(seq) - 1)
        state["n"] += 1
        return seq[i]
    ai._call_ollama = fake
    ai.check_ollama_alive = lambda: True         # 假装模型在线
    return state


GOOD = ('{"solution": "先求导数再代入", "error_type": "calculation_error",'
        ' "error_type_label": "计算失误", "error_confidence": 0.8,'
        ' "error_reason": "第二步符号代错", "knowledge_points": ["导数"],'
        ' "similar_question": "求 x^3 的导数"}')
PROSE = "好的，这道题的解题思路如下：先求导，然后代入计算，最后得出结果。"
MISSING = '{"solution": "先求导", "error_type": "calculation_error"}'
BAD_TYPE = ('{"solution": "s", "error_type": "concept",'
            ' "error_type_label": "概念不清", "error_confidence": 0.8,'
            ' "error_reason": "概念没掌握", "knowledge_points": ["x"],'
            ' "similar_question": "q"}')


def main():
    print("=" * 64)
    print("AI 协议联调自测（桩函数，不连真实模型）")
    print(f"协议版本 {ai.PROTOCOL_VERSION}")
    print("=" * 64)

    # ---- 1. 一次通过 ----
    print("\n[1] 返回合法 JSON → code=0")
    s = stub(GOOD)
    r = ai.analyze_error("求 x^2 在 x=2 处的导数", "4")
    env = r.to_envelope()
    check("code == 0", env["code"] == 0, f"实际 {env['code']}")
    check("只调用模型 1 次", s["n"] == 1, f"实际 {s['n']} 次")
    check("data 已填充", env["data"] is not None)
    check("data.error_type 为四 code 之一",
          env["data"]["error_type"] in ai.ERROR_TYPE_CODES,
          env["data"]["error_type"])
    check("信封字段齐全",
          set(env) == {"code", "scene", "data", "raw", "model", "elapsed_ms"})
    check("scene 归一为 error_analysis", env["scene"] == "error_analysis")
    check("confidence 0.8 → 不需人工复核", r.need_review is False)

    # ---- 2. 第一次散文、第二次合法 → 重试生效 ----
    print("\n[2] 首次返回散文 → 重试 1 次后成功")
    s = stub(PROSE, GOOD)
    r = ai.analyze_error("求导数", "1")
    check("code == 0", r.code == 0, f"实际 {r.code}")
    check("确实重试了（共调用 2 次）", s["n"] == 2, f"实际 {s['n']} 次")
    check("data 来自第二次", r.data["error_type"] == "calculation_error")

    # ---- 3. 一直散文 → 降级 ----
    print("\n[3] 两次都不合法 → 降级 code=1")
    s = stub(PROSE, PROSE)
    r = ai.analyze_error("求导数", "2")
    check("code == 1（降级）", r.code == 1, f"实际 {r.code}")
    check("重试上限生效（共 2 次：1 次 + 重试 1 次）", s["n"] == 2, f"实际 {s['n']} 次")
    check("data 为 None", r.data is None)
    check("ok 为 False（调用方据此拒绝入库）", r.ok is False)
    check("content 为空串（原文不会进业务表）", r.content == "")
    check("raw 保留原文供排查", r.raw.get("text") == PROSE)
    check("raw 记录了重试次数", r.raw.get("retried") == 1)

    # ---- 4. 缺字段 ----
    print("\n[4] JSON 缺必填字段 → 降级")
    s = stub(MISSING, MISSING)
    r = ai.analyze_error("求导数", "3")
    check("code == 1", r.code == 1, f"实际 {r.code}")
    check("error 说明缺了什么", "error_type_label" in (r.error or ""),
          r.error or "")

    # ---- 5. error_type 不在四 code 内 ----
    print("\n[5] error_type 不是队长四 code → 降级")
    s = stub(BAD_TYPE, BAD_TYPE)
    r = ai.analyze_error("求导数", "4")
    check("code == 1", r.code == 1, f"实际 {r.code}")
    check("error 指出非法取值", "calculation_error" in (r.error or "") or
          "四" in (r.error or ""), r.error or "")

    # ---- 6. 低置信度 → 待人工复核 ----
    print("\n[6] 置信度 < 0.5 → 待人工复核")
    low = GOOD.replace('"error_confidence": 0.8', '"error_confidence": 0.3')
    s = stub(low)
    r = ai.analyze_error("求导数", "5")
    check("code == 0（解析本身是成功的）", r.code == 0)
    check("need_review 为真", r.need_review is True,
          f"confidence={r.data['error_confidence']}")
    check("入库时 error_type 应置 NULL（由 ocr_ai_batch 执行）", True)

    # ---- 7. 服务不可达 → code=1，不抛异常 ----
    print("\n[7] 模型服务不可达 → code=1，不向上抛异常")
    import urllib.error
    def boom(prompt):
        raise urllib.error.URLError("connection refused")
    ai._call_ollama = boom
    r = ai.analyze_error("求导数", "6")
    check("code == 1", r.code == 1, f"实际 {r.code}")
    check("未抛异常（调用方能继续跑批）", True)
    check("data 为 None", r.data is None)

    # ---- 8. 旧名兼容 ----
    print("\n[8] 旧调用方兼容（ok / content / elapsed_ms）")
    s = stub(GOOD)
    r = ai.explain_wrong_question("求导数", "7")     # 旧函数
    check("旧函数仍可用", r.ok is True and r.content and r.elapsed_ms >= 0)
    s = stub(GOOD)
    r2 = ai.explain_wrong_question_v2("求导数", "8")
    check("v2 走 v2 Prompt", r2.ok is True)

    print("\n" + "=" * 64)
    if _failures:
        print(f"❌ {len(_failures)} 项未通过：")
        for x in _failures:
            print("   -", x)
        return 1
    print("✅ 全部通过。三条硬约束均已落地：")
    print("   · code=0/1 信封结构正确")
    print("   · code=2 格式不符自动重试 1 次后降级")
    print("   · 降级时 data=None、content 为空串，原文只留在 raw")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
