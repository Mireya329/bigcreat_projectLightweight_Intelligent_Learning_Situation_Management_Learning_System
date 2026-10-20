# -*- coding: utf-8 -*-
"""id15 真实模型复测（网关 v2.2）。

队长 v2.2 第五节：「占位符问题：网关规则已修正并通过三份历史输出回放，
真实模型复测待二号执行」。本脚本就是那次真实复测——走 HTTP 网关调真实 7B，
保留完整原始信封供复核。

⚠️ 两点如实声明：
  1. 库里 id15 的题干是 OCR 残片（含 "-2x / 2y / x+y / (x-y/^2+(x+y)2}" 等碎片），
     未经原图核对，**解题正确性不作任何结论**，只验证协议行为；
  2. id15 没有学生作答（wrong_answer_text 为空），所以置信度必为 0，
     这是降级分支，不是分类能力的证据。

用法：python tools/retest_id15_v22.py --runs 2
     （默认用 data/learning_test_v22.db；--db 可改；只读，不写库）
"""
import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY = "http://127.0.0.1:8765/ai"
OUT_DIR = ROOT / "docs" / "logs"
TIMEOUT = 370


def call(envelope: dict, timeout=TIMEOUT):
    req = urllib.request.Request(
        GATEWAY, data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--db", default=str(ROOT / "data" / "learning_test_v22.db"))
    ap.add_argument("--id", type=int, default=15)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, question_text, wrong_answer_text, answer_text, error_type,"
        " review_flag FROM error_items WHERE id=?", (args.id,)).fetchone()
    conn.close()
    if row is None:
        sys.exit(f"{args.db} 里没有 id={args.id}")

    print(f"id={args.id}  题干 {len(row['question_text'] or '')} 字"
          f"  作答={'有' if (row['wrong_answer_text'] or '').strip() else '无'}"
          f"  参考答案={'有' if (row['answer_text'] or '').strip() else '无'}")

    runs = []
    for i in range(args.runs):
        env = {"scene": "error_analysis",
               "input": {"question": row["question_text"] or "",
                         "student_answer": row["wrong_answer_text"] or ""},
               "subject": "math", "user_context": {}}
        t0 = time.time()
        resp = call(env)
        elapsed = round(time.time() - t0, 1)
        data = resp.get("data")
        raw = resp.get("raw") or {}
        runs.append({"run": i + 1, "elapsed_s": elapsed, "envelope": resp,
                     "raw_text": raw.get("text", "")})
        print(f"  第{i + 1}次: code={resp.get('code')}  retried={raw.get('retried')}"
              f"  用时{elapsed}s")
        if data:
            print(f"     answer={str(data.get('answer'))[:40]!r}"
                  f"  conf={data.get('error_confidence')}"
                  f"  type={data.get('error_type')}")
            print(f"     normalizations={raw.get('normalizations')}")
        else:
            print(f"     失败原因: {raw.get('reason')}")
            print(f"     answer_evidence={raw.get('answer_evidence')}")
        print(f"     model={resp.get('model')} protocol={resp.get('protocol', {}).get('version')}")

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out = OUT_DIR / f"id{args.id}_v22_real_{stamp}.json"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(
        {"item_id": args.id, "db": args.db, "gateway": GATEWAY,
         "note": "题干为 OCR 残片未经原图核对，仅验证协议行为，不评估解题正确性",
         "stem": row["question_text"],
         "student_answer": row["wrong_answer_text"],
         "runs": runs}, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\n原始记录已存：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
