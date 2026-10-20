#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""计划参数两入口一致性验证（队长 v2.2 第六节遗留项）

背景：网关 captain_ai.normalize 与后端 ai_interface.normalize_user_context
各自有一套 plan_days / available_minutes_per_day 处理，非法类型时行为不同
（网关 raise → code=2，后端悄悄改默认继续跑）。本脚本逐值对照两侧，
再用真实 HTTP 打网关做端到端比对，确认统一后两边同码同因。

用法（需网关已在 127.0.0.1:8765 运行）：
    python tools/verify_plan_param_parity.py
    python tools/verify_plan_param_parity.py --no-http   # 只做单元级对照
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src" / "backend"))
sys.path.insert(0, str(ROOT / "vendor" / "captain_ai_v2"))

import ai_interface as ai                                    # noqa: E402

GATEWAY = os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8765/ai")

# 每个用例：(说明, 给定值)
CASES = [
    ("合法整数 5",            5),
    ("越界上限 99",           99),
    ("越界下限 -5",           -5),
    ("整数字符串 '60'",       "60"),
    ("带空格 ' 60 '",        " 60 "),
    ("非法字符串 'abc'",      "abc"),
    ("带单位 '3天'",          "3天"),
    ("布尔 True",            True),
    ("小数 60.5",             60.5),
    ("空列表 []",             []),
    ("缺失 None",             None),
]

PASS = FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    print(("  ✓ " if ok else "  ✗ ") + name + (f"  {detail}" if detail else ""))
    if ok:
        PASS += 1
    else:
        FAIL += 1


def gateway_unit(value):
    """网关侧处理：直接调 captain_ai.normalize（in-process，不跑模型）。

    返回 ('ok', 生效值) 或 ('reject', 原因)。网关对非法类型是 raise，
    转成 code=2 由调用方（server）包装，这里记 reject。
    """
    import captain_ai as cap
    req = {"scene": "qa", "subject": "math", "input": {"question": "复习高数"},
           "user_context": {"task": "study_plan", "plan_days": value}}
    try:
        norm, _task = cap.normalize(dict(req))
        return ("ok", norm["user_context"]["plan_days"])
    except ValueError as e:
        return ("reject", str(e))


def backend_unit(value, plan=True):
    """我侧处理：normalize_user_context，plan=True 表示 study_plan 场景。"""
    uc = {"plan_days": value}
    if plan:
        uc["task"] = "study_plan"
    out = ai.normalize_user_context(uc, scene="qa")
    invalid = list(ai.LAST_USER_CONTEXT_INVALID)
    if invalid:
        return ("reject", "plan_days must be an integer"), out["plan_days"]
    return ("ok", out["plan_days"]), out["plan_days"]


def http_gateway(value):
    """真实 HTTP 打网关，返回 (code, reason)。网关没起返回 None。"""
    payload = {"scene": "qa", "subject": "math", "input": {"question": "复习高数"},
               "user_context": {"task": "study_plan", "plan_days": value}}
    data = json.dumps(payload, ensure_ascii=False).encode()
    r = urllib.request.Request(GATEWAY, data=data,
                               headers={"Content-Type": "application/json"},
                               method="POST")
    try:
        resp = json.loads(urllib.request.urlopen(r, timeout=370).read().decode())
        return resp.get("code"), str((resp.get("raw") or {}).get("reason", ""))
    except urllib.error.HTTPError as e:
        return None, f"HTTPError {e.code}"
    except Exception as e:                                  # 网关没起
        return None, f"unreachable: {type(e).__name__}"


print("=" * 70)
print("一、单元级对照：网关 captain_ai.normalize  vs  我侧 normalize_user_context")
print("=" * 70)
print(f"{'用例':<16}{'网关':<28}{'我侧(study_plan)':<28}{'一致'}")
print("-" * 70)
for label, value in CASES:
    g = gateway_unit(value)
    b, eff = backend_unit(value)
    same = (g[0] == b[0]) and (g[0] == "reject" or g[1] == b[1])
    gs = "拒绝(code=2)" if g[0] == "reject" else f"生效 {g[1]}"
    bs = "拒绝(code=2)" if b[0] == "reject" else f"生效 {b[1]}"
    print(f"{label:<16}{gs:<28}{bs:<28}{'✅' if same else '❌'}")
    check(f"[{label}] 两侧一致", same, f"网关={gs} 我侧={bs}")

print()
print("=" * 70)
print("二、非 study_plan 场景：网关不读这两个键，我侧保持降级改默认")
print("=" * 70)
for label, value in [("非法字符串 'abc'", "abc"), ("布尔 True", True),
                     ("小数 60.5", 60.5)]:
    b, eff = backend_unit(value, plan=False)
    ok = (b[0] == "ok") and (eff == ai.PLAN_DAYS_DEFAULT)
    check(f"[{label}] 降级为默认 {ai.PLAN_DAYS_DEFAULT} 且不拦", ok,
          f"生效 {eff}，invalid={ai.LAST_USER_CONTEXT_INVALID}")
    # 交叉验证：网关在非 study_plan 场景确实不校验
    import captain_ai as cap
    req = {"scene": "error_analysis", "subject": "math",
           "input": {"question": "求极限"},
           "user_context": {"student_answer": "", "plan_days": value}}
    try:
        norm, _ = cap.normalize(dict(req))
        check(f"[{label}] 网关 error_analysis 也不校验", True,
              f"网关放行，plan_days={norm['user_context'].get('plan_days')}")
    except ValueError as e:
        check(f"[{label}] 网关 error_analysis 也不校验", False, f"网关却拒了：{e}")

print()
print("=" * 70)
print("三、端到端：我侧 analyze/study_plan 调 call_ai 时被本地拦下（不调模型）")
print("=" * 70)
calls = {"n": 0}
_orig = ai.urllib.request.urlopen


class _Fake:
    def __init__(self, b):
        self._b = b

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _spy(req, timeout=None):
    calls["n"] += 1
    real = _orig(req, timeout=timeout)
    return _Fake(real.read())


ai.urllib.request.urlopen = _spy
try:
    for label, value in [("非法字符串 'abc'", "abc"), ("带单位 '3天'", "3天"),
                         ("布尔 True", True), ("小数 60.5", 60.5)]:
        calls["n"] = 0
        resp = ai.study_plan("复习高数", plan_days=value)
        check(f"[{label}] code=2 拦下", resp.code == 2, f"code={resp.code}")
        check(f"[{label}] 未发出 HTTP（没调模型）", calls["n"] == 0,
              f"调用 {calls['n']} 次")
        check(f"[{label}] 原因写明 must be an integer",
              "must be an integer" in (resp.raw.get("detail") or ""),
              str(resp.raw.get("detail")))

    # 合法值不误伤：越界夹取，不拦
    calls["n"] = 0
    ai.urllib.request.urlopen = _orig        # 合法值会真发请求，还原
    resp = ai.study_plan("复习高数", plan_days=99, minutes_per_day=999)
    check("[越界 99/999] 不拦，夹取后正常走", resp.code != 2,
          f"code={resp.code}（{resp.error or '正常'}）")
    check("[越界 99/999] 夹取记录可见",
          ai.LAST_USER_CONTEXT_NOTES.get("plan_days", {}).get("生效值") == 7
          and ai.LAST_USER_CONTEXT_NOTES.get(
              "available_minutes_per_day", {}).get("生效值") == 480,
          json.dumps(ai.LAST_USER_CONTEXT_NOTES, ensure_ascii=False))
    check("[字符串 '5'] 可转换不被拒",
          ai.normalize_user_context({"task": "study_plan", "plan_days": "5"},
                                    scene="qa")["plan_days"] == 5)
    check("[字符串 '5'] 不进 invalid",
          ai.LAST_USER_CONTEXT_INVALID == [], str(ai.LAST_USER_CONTEXT_INVALID))
finally:
    ai.urllib.request.urlopen = _orig

print()
print("=" * 70)
print("四、真实 HTTP 打网关：网关返回 vs 我侧本地拦下，码与原因一致")
print("=" * 70)
if "--no-http" in sys.argv:
    print("  （跳过：--no-http）")
else:
    for label, value in [("非法字符串 'abc'", "abc"), ("布尔 True", True)]:
        code, reason = http_gateway(value)
        if code is None:
            print(f"  ⚠ 网关不可达（{reason}），跳过 {label}")
            continue
        calls["n"] = 0
        ai.urllib.request.urlopen = _spy
        try:
            resp = ai.study_plan("复习高数", plan_days=value)
        finally:
            ai.urllib.request.urlopen = _orig
        check(f"[{label}] 网关 code=2", code == 2, f"网关 code={code} reason={reason}")
        check(f"[{label}] 我侧同为 code=2 且未发请求",
              resp.code == 2 and calls["n"] == 0,
              f"我侧 code={resp.code}，HTTP 调用 {calls['n']} 次")

print()
print("=" * 70)
print(f"结果：{PASS} 项通过，{FAIL} 项失败")
print("=" * 70)
sys.exit(1 if FAIL else 0)
