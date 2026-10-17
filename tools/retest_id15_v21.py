# -*- coding: utf-8 -*-
"""v2.1 空答案兜底复测：在**测试库副本**上复测 id15，保留原始模型响应。

队长第三轮回复第五节第 4 条要求：
  · 在备份得到的测试数据库上复测 id15
  · 记录原始模型输出（raw.text）、补全信息（raw.normalizations）和最终状态
  · 先保留原始记录，不清空历史数据

本脚本只读测试库，不连正式库、不写任何数据库。
用法：
    python tools/retest_id15_v21.py            # 默认跑 3 次
    python tools/retest_id15_v21.py --runs 1   # 只跑 1 次
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
TEST_DB = ROOT / "data" / "learning_test_v21.db"
GATEWAY = "http://127.0.0.1:8765/ai"
OUT_DIR = ROOT / "docs" / "logs"
TIMEOUT = 370


def load_item(item_id: int) -> dict:
    """从测试库副本读一条错题。缺作答就是缺作答，不编造。"""
    if not TEST_DB.exists():
        sys.exit(f"测试库不存在：{TEST_DB}\n请先跑备份（sqlite3 backup）。")
    conn = sqlite3.connect(TEST_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM error_items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if row is None:
        sys.exit(f"测试库里没有 id={item_id}")
    return dict(row)


def build_request(item: dict) -> dict:
    """按队长协议组请求：input 用对象，只放白名单字段。"""
    question = (item.get("question_text") or item.get("ocr_text") or "").strip()
    student_answer = (item.get("wrong_answer_text") or "").strip()
    body_input = {"question": question, "student_answer": student_answer}
    if item.get("answer_text"):
        body_input["reference_answer"] = str(item["answer_text"]).strip()
    return {
        "scene": "error_analysis",
        "subject": "math",
        "input": body_input,
        "user_context": {},
    }


def call(body: dict) -> tuple[dict, float]:
    req = urllib.request.Request(
        GATEWAY, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode("utf-8")), (time.time() - t0) * 1000


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--id", type=int, default=15)
    ap.add_argument("--runs", type=int, default=3)
    args = ap.parse_args()

    item = load_item(args.id)
    body = build_request(item)
    print(f"复测 id={args.id}（测试库 {TEST_DB.name}）")
    print(f"  题干长度 {len(body['input']['question'])} 字符")
    print(f"  学生作答 {'（空）' if not body['input']['student_answer'] else body['input']['student_answer'][:50]}")
    print(f"  网关 {GATEWAY}\n")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    raw_path = OUT_DIR / f"id{args.id}_v21_raw_{stamp}.json"

    records = []
    for i in range(1, args.runs + 1):
        try:
            env, ms = call(body)
        except Exception as exc:
            print(f"  第 {i} 次：请求失败 {exc}")
            records.append({"run": i, "error": str(exc)})
            continue
        raw = env.get("raw") or {}
        data = env.get("data") or {}
        norms = raw.get("normalizations") or []
        print(f"  第 {i} 次：code={env.get('code')}  "
              f"耗时={ms:.0f}ms  answer={'（空）' if not str(data.get('answer') or '').strip() else repr(str(data.get('answer'))[:40])}  "
              f"error_type={data.get('error_type')}  conf={data.get('error_confidence')}")
        if norms:
            for n in norms:
                print(f"          ↳ 补全 {n.get('field')}: {str(n.get('value'))[:60]!r} "
                      f"(来源 {n.get('source')})")
        else:
            print("          ↳ 无 normalizations（未触发兜底）")
        records.append({
            "run": i, "elapsed_ms": round(ms), "envelope": env,
            "raw_text": raw.get("text", ""), "normalizations": norms,
        })

    # 原始响应落盘，供后续排查和反馈队长
    out = {
        "item_id": args.id,
        "test_db": str(TEST_DB),
        "protocol": (records[0].get("envelope", {}) or {}).get("protocol") if records else None,
        "request": body,
        "item_snapshot": {
            "question_text": item.get("question_text"),
            "wrong_answer_text": item.get("wrong_answer_text"),
            "answer_text": item.get("answer_text"),
            "review_flag": item.get("review_flag"),
            "error_type": item.get("error_type"),
            "error_confidence": item.get("error_confidence"),
        },
        "runs": records,
    }
    raw_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始响应已保存：{raw_path}")

    codes = [r.get("envelope", {}).get("code") for r in records if "envelope" in r]
    ok = sum(1 for c in codes if c == 0)
    print(f"结果：{ok}/{len(codes)} 次 code=0；"
          f"code 分布 { {c: codes.count(c) for c in set(codes)} }")


if __name__ == "__main__":
    main()
