# -*- coding: utf-8 -*-
"""用队长交付包的 10 项样例，真打网关跑一遍。

和 tools/_probe_gateway.py 的区别：
  _probe_gateway 只挑 3 条探活，这里把 examples.json 全量跑完并出表格，
  结果直接作为回传给队长第 3 项材料（"10 项样例的实际结果"）。

用法：
    venv/Scripts/python.exe tools/run_captain_examples.py
前提：队长网关已在 127.0.0.1:8765 起了（vendor/captain_ai_v2/server.py）
"""
import json
import os
import sys
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES = os.path.join(ROOT, "vendor", "captain_ai_v2", "examples.json")
GATEWAY = os.getenv("AI_GATEWAY_URL", "http://127.0.0.1:8765/ai")
TIMEOUT = int(os.getenv("AI_GATEWAY_TIMEOUT", "370"))


def call(req: dict) -> tuple[dict, float]:
    body = json.dumps(req, ensure_ascii=False).encode("utf-8")
    http = urllib.request.Request(
        GATEWAY, data=body, method="POST",
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    with urllib.request.urlopen(http, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8")), (time.time() - t0) * 1000


def brief(env: dict, scene: str, task: str) -> str:
    """从信封里摘一句人话摘要，别整段 JSON 糊在表格里。"""
    code = env.get("code")
    if code != 0:
        raw = env.get("raw") or {}
        reason = raw.get("reason") if isinstance(raw, dict) else str(raw)
        return f"code={code} {reason or ''}"[:60]
    d = env.get("data") or {}
    if scene == "error_analysis":
        label = d.get("error_type_label") or d.get("error_type")
        return f"{label} 置信度{d.get('error_confidence')}"
    if task == "study_plan":
        plan = d.get("plan") or []
        return f"{len(plan)} 天计划"
    if task == "politics_quiz":
        return str(d.get("feedback") or d.get("verdict") or "")[:40]
    if scene == "essay_review":
        return f"改后全文 {len(str(d.get('revised_essay') or ''))} 字，" \
               f"{len(d.get('corrections') or [])} 处修正"
    if scene == "weak_diagnosis":
        return f"{len(d.get('weak_points') or [])} 个薄弱点，" \
               f"{len(d.get('suggestions') or [])} 条建议"
    if scene == "school_recommend":
        return f"冲{len(d.get('reach') or [])}/稳{len(d.get('match') or [])}" \
               f"/保{len(d.get('safety') or [])}"
    return str(d.get("answer") or "")[:40]


def main() -> int:
    if not os.path.exists(EXAMPLES):
        print(f"找不到样例文件：{EXAMPLES}")
        return 1

    with open(EXAMPLES, encoding="utf-8") as f:
        cases = json.load(f)

    print(f"共 {len(cases)} 项样例，网关 {GATEWAY}")
    print(f"协议版本以网关 /schema 为准，超时 {TIMEOUT}s")
    print("=" * 78)
    print(f"{'#':<3}{'名称':<12}{'场景':<16}{'结果':<34}{'耗时'}")
    print("-" * 78)

    rows, fails = [], 0
    for i, case in enumerate(cases, 1):
        name = case.get("name", "")
        req = case["request"]
        scene = req.get("scene", "")
        task = (req.get("user_context") or {}).get("task", "")
        try:
            env, ms = call(req)
        except Exception as e:                                  # noqa: BLE001
            rows.append((name, scene, f"调用异常 {type(e).__name__}", 0))
            fails += 1
            print(f"{i:<3}{name:<12}{scene:<16}{'调用异常':<34}")
            continue

        # 样例自带 expected_code 的（如"请求字段错误"期望 2、"择校暂不开放"
        # 期望 1）按预期判；没有的才默认期望 0。别把预期内的失败算成 bug。
        expect = case.get("expected_code", 0)
        ok = env.get("code") == expect
        if not ok:
            fails += 1
            print(f"    ⚠ 期望 code={expect}，实际 {env.get('code')}")
        # CPU 上 7b 一题几秒，<1s 说明没走模型推断。
        # 网关对 weak_diagnosis 是**故意**不调模型的（用确定性统计规则算，
        # model=deterministic-statistics-v2），这是设计不是故障，标注清楚。
        gmodel = str(env.get("model") or "")
        if gmodel.startswith("deterministic"):
            note = "  （统计规则，网关设计上不调模型）"
        elif ms < 1000 and expect == 0:
            note = "  ⚠ 未走模型推断，需查明"
        else:
            note = ""
        suspicious = note
        rows.append((name, scene, brief(env, scene, task), ms, suspicious))
        print(f"{i:<3}{name:<12}{scene:<16}{brief(env, scene, task):<34}"
              f"{ms/1000:.1f}s{suspicious}")

    print("=" * 78)
    print(f"符合预期 {len(rows) - fails}/{len(rows)}")

    out = os.path.join(ROOT, "docs", "10项样例实测结果.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write("# 队长交付包 10 项样例实测结果\n\n")
        f.write(f"- 网关：{GATEWAY}\n- 用例来源：vendor/captain_ai_v2/examples.json\n")
        f.write(f"- 符合预期 {len(rows) - fails}/{len(rows)}"
                f"（带 expected_code 的样例按样例预期判定）\n\n")
        f.write("| # | 名称 | 场景 | 结果 | 耗时 |\n|---|---|---|---|---|\n")
        for i, row in enumerate(rows, 1):
            name, scene, res, ms = row[0], row[1], row[2], row[3]
            note = row[4] if len(row) > 4 else ""
            f.write(f"| {i} | {name} | {scene} | {res} | {ms/1000:.1f}s{note} |\n")
    print(f"\n已写入 {out}")
    return 0 if fails == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
