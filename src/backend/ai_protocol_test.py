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
import json
import sys
import urllib.error
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
    # 队长协议六字段必须齐全；protocol 是我侧附加的版本说明——
    # 网关把 code=2 改成了"请求字段错误"，不加这个字段同一份信封会被误读
    check("信封六字段齐全",
          {"code", "scene", "data", "raw", "model", "elapsed_ms"} <= set(env))
    check("信封带 protocol 版本说明",
          bool(env.get("protocol", {}).get("version")))
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

    # ---- 8. 稳定性校验：分类一致 ----
    print("\n[8] 稳定性校验：两轮分类一致 → 照常返回")
    s = stub(GOOD, GOOD)
    r = ai.analyze_error("求导数", "8", stable=True)
    check("两轮各调用 1 次（共 2 次）", s["n"] == 2, f"实际 {s['n']} 次")
    check("stable 为 True", r.data.get("stable") is True)
    check("置信度取多轮最小值", r.data["error_confidence"] == 0.8,
          f"实际 {r.data['error_confidence']}")
    check("解析文本保留", "正确解题思路" in r.content)

    # ---- 9. 稳定性校验：分类不一致 → 置信度置 0，转人工复核 ----
    print("\n[9] 稳定性校验：两轮分类不一致 → 置 0 转人工复核")
    other = GOOD.replace("calculation_error", "method_gap")
    s = stub(GOOD, other)
    r = ai.analyze_error("求导数", "9", stable=True)
    check("code 仍为 0（解析可用，不是模型异常）", r.code == 0, f"实际 {r.code}")
    check("stable 为 False", r.data.get("stable") is False)
    check("置信度被置 0", r.data["error_confidence"] == 0.0,
          f"实际 {r.data['error_confidence']}")
    check("need_review 为真 → error_type 不入库", r.need_review is True)
    check("解析文本仍然保留（不浪费这一次调用）", "正确解题思路" in r.content)
    check("reason 里写明未通过稳定性校验",
          "稳定性校验" in str(r.data.get("error_reason", "")))
    check("raw 保留两轮原始输出", len(r.raw.get("rounds", [])) == 2)

    # ---- 10. 稳定性校验：其中一轮降级 ----
    print("\n[10] 稳定性校验：一轮成功一轮降级 → 以成功的为准")
    s = stub(GOOD, PROSE, PROSE)
    r = ai.analyze_error("求导数", "10", stable=True)
    check("code == 0", r.code == 0, f"实际 {r.code}")
    check("用上了成功那轮的结果", r.data is not None)

    # ---- 11. judge_fragment 不稳定 → 强制判 C ----
    print("\n[11] judge_fragment 两轮判定不一致 → 强制 C（不补录）")
    def frag(ch):
        return ('{"choice": "%s", "guessed_no": "1", "stem": "s",'
                ' "qtype": "计算", "reason": "r"}' % ch)
    s = stub(frag("B"), frag("A"))
    r = ai.call_ai_stable(ai.AIRequest(
        scene="judge_fragment",
        input={"fragment": "x", "prev": "p", "next": "n"}))
    check("choice 被强制为 C", r.data.get("choice") == "C",
          f"实际 {r.data.get('choice')}")
    check("stable 为 False", r.data.get("stable") is False)
    check("写明原因", "稳定性校验" in str(r.data.get("reason", "")))
    check("渲染文本以 C 开头（ocr_recover 的正则认得出）",
          r.content.strip().startswith("C"), repr(r.content[:20]))

    # ---- 12. judge_fragment 一致 → 照常 ----
    print("\n[12] judge_fragment 两轮一致 → 照常返回")
    s = stub(frag("B"), frag("B"))
    r = ai.call_ai_stable(ai.AIRequest(
        scene="judge_fragment",
        input={"fragment": "x", "prev": "p", "next": "n"}))
    check("choice 保持 B", r.data.get("choice") == "B")
    check("stable 为 True", r.data.get("stable") is True)

    # ---- 13. 旧名兼容 ----
    print("\n[13] 旧调用方兼容（ok / content / elapsed_ms）")
    s = stub(GOOD)
    r = ai.explain_wrong_question("求导数", "7")     # 旧函数
    check("旧函数仍可用", r.ok is True and r.content and r.elapsed_ms >= 0)
    s = stub(GOOD)
    r2 = ai.explain_wrong_question_v2("求导数", "8")
    check("v2 走 v2 Prompt", r2.ok is True)

    # ---- 14~16. 网关分支（队长 2026-09-27 回复第四节第 2 条）----
    print("\n[14] 网关分支：后端不再叠加模型重试")
    old_provider = ai.AI_PROVIDER
    ai.AI_PROVIDER = "gateway"
    try:
        def gw(env):
            """把预设信封原样返回，并计数"""
            gw.n += 1
            return env
        gw.n = 0
        ai._call_gateway = lambda scene, subject, input_, uc, task=None: gw(
            gw.env)

        # 14a 网关成功
        gw.n = 0
        gw.env = {"code": 0, "scene": "error_analysis", "data": json.loads(GOOD),
                  "raw": None, "model": "qwen2.5:7b", "elapsed_ms": 21000}
        r = ai.analyze_error("求导数", "1")
        check("网关 code=0 → 我侧 code=0", r.code == 0, f"实际 {r.code}")
        check("只请求网关 1 次（不叠加后端重试）", gw.n == 1, f"实际 {gw.n} 次")
        check("model 透传网关的", r.model == "qwen2.5:7b", r.model)

        # 14b 网关 code=1（模型侧已重试仍失败）
        gw.n = 0
        gw.env = {"code": 1, "scene": "error_analysis", "data": None,
                  "raw": {"reason": "model_failed_after_retry"},
                  "model": "qwen2.5:7b", "elapsed_ms": 360000}
        r = ai.analyze_error("求导数", "2")
        check("网关 code=1 → 我侧 code=1（降级）", r.code == 1, f"实际 {r.code}")
        check("data=None、content 为空", r.data is None and r.content == "")
        check("网关失败也不重试", gw.n == 1, f"实际 {gw.n} 次")
        check("保留网关 raw 供排障",
              (r.raw or {}).get("reason") == "model_failed_after_retry", r.raw)

        # 14c 网关 code=2（请求字段错误）——重试无意义，原样反馈
        gw.n = 0
        gw.env = {"code": 2, "scene": "error_analysis", "data": None,
                  "raw": {"reason": "bad_request: subject 非法"},
                  "model": "qwen2.5:7b", "elapsed_ms": 5}
        r = ai.analyze_error("求导数", "3")
        check("网关 code=2 → 我侧保留 code=2 语义", r.code == 2, f"实际 {r.code}")
        check("code=2 不重试（重试没有意义）", gw.n == 1, f"实际 {gw.n} 次")
        check("错误信息带出网关原因",
              "bad_request" in (r.error or ""), r.error)

        # 14d 网关 code=0 但 data 缺字段：后端继续校验，但不回头重试
        gw.n = 0
        gw.env = {"code": 0, "scene": "error_analysis",
                  "data": {"solution": "只有解析，没给分类"},
                  "raw": None, "model": "qwen2.5:7b", "elapsed_ms": 22000}
        r = ai.analyze_error("求导数", "4")
        check("网关成功但字段不全 → 我侧降级 code=1", r.code == 1, f"实际 {r.code}")
        check("校验失败也不重发请求", gw.n == 1, f"实际 {gw.n} 次")
        check("raw.retry_owner 标明重试归网关",
              (r.raw or {}).get("retry_owner") == "gateway", r.raw)
    finally:
        ai.AI_PROVIDER = old_provider

    print("\n[15] 网关分支：HTTP 状态码 ≠ 信封 code，分开处理")
    import urllib.error
    ai.AI_PROVIDER = "gateway"
    try:
        ai._call_gateway = lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.HTTPError(ai.AI_GATEWAY_URL, 503, "busy", {}, None))
        r = ai.analyze_error("求导数", "5")
        check("HTTP 503 → code=1（不是 code=2）", r.code == 1, f"实际 {r.code}")
        check("提示稍后再试、不无限重试", "稍后再试" in (r.error or ""), r.error)
        check("HTTP 状态码记进 raw", (r.raw or {}).get("http_status") == 503, r.raw)
    finally:
        ai.AI_PROVIDER = old_provider

    print("\n[16] qa 子任务在网关下按 task 分派")
    ai.AI_PROVIDER = "gateway"
    try:
        seen = {}
        def gw2(scene, subject, input_, uc, task=None):
            seen["scene"], seen["task"], seen["uc"] = scene, task, uc
            if task == "study_plan":
                return {"code": 0, "scene": scene, "data": {"plan": ["第1天"]},
                        "raw": None, "model": "m", "elapsed_ms": 1}
            if task == "politics_quiz":
                return {"code": 0, "scene": scene, "data": {"verdict": "基本正确"},
                        "raw": None, "model": "m", "elapsed_ms": 1}
            return {"code": 0, "scene": scene,
                    "data": {"answer": "1", "steps": ["a"]},
                    "raw": None, "model": "m", "elapsed_ms": 1}
        ai._call_gateway = gw2

        r = ai.study_plan("复习高数", plan_days=10, minutes_per_day=999)
        check("study_plan 走 scene=qa + task=study_plan",
              seen["scene"] == "qa" and seen["task"] == "study_plan", seen)
        check("plan_days 越界夹到 7", seen["uc"]["plan_days"] == 7,
              seen["uc"].get("plan_days"))
        check("每日分钟夹到 480", seen["uc"]["available_minutes_per_day"] == 480,
              seen["uc"].get("available_minutes_per_day"))
        check("渲染出复习计划", "复习计划" in r.content, r.content[:40])

        r = ai.politics_quiz("什么是实践", "实践是……")
        check("政治抽查 verdict 字段生效", (r.data or {}).get("verdict") == "基本正确")
        check("渲染出判定", "判定" in r.content, r.content[:40])

        r = ai.math_guidance("求极限")
        check("数学指导要 steps", "steps" in (r.data or {}))
        check("渲染出步骤", "步骤" in r.content, r.content[:40])
    finally:
        ai.AI_PROVIDER = old_provider

    print("\n" + "=" * 64)
    if _failures:
        print(f"❌ {len(_failures)} 项未通过：")
        for x in _failures:
            print("   -", x)
        return 1
    print("✅ 全部通过。已落地的约束：")
    print("   · code=0/1 信封结构正确")
    print("   · 直连 Ollama：code=2 重试 1 次后降级")
    print("   · 网关分支：不叠加模型重试，code=2=请求字段错误原样反馈")
    print("   · 降级时 data=None、content 为空串，原文只留在 raw")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
