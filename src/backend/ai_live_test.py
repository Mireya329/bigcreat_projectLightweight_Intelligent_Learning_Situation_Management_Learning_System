#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""协议真机联调（需 Ollama 在线）。

回答一个问题：format=json 在 qwen2.5:0.5b 上到底能不能逼出合法 JSON？

分两层测：
  A 层  裸测 Ollama 的 format=json —— 模型本身行不行
  B 层  走我侧 call_ai 全流程 —— 校验/重试/降级这条链子行不行

用法：
    venv\\Scripts\\python.exe src/backend/ai_live_test.py
    venv\\Scripts\\python.exe src/backend/ai_live_test.py --n 5   # 只跑前 5 题

结果写进 docs/真机联调记录.md，负面结果同样记录。
"""
import json
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))

import ai_interface as ai
from db import get_conn

OLLAMA = ai.OLLAMA_BASE_URL
MODEL = ai.MODEL_NAME

# A 层用的极简 prompt：连 0.5b 都该答对的问题，用来单纯检验格式能力
A_PROMPTS = [
    ("四选一",
     '从 A、B、C、D 中选一个字母，只输出 JSON：{"choice": "A"}\n'
     '问题：1+1=?  A.2  B.3  C.4  D.5'),
    ("多字段",
     '只输出 JSON，字段：{"answer": 字符串, "confidence": 0到1的小数}。\n'
     '问题：中国的首都是哪座城市？'),
    ("错题分类",
     '只输出 JSON，字段：{"solution": 字符串, "error_type": '
     '只能是 concept_misunderstanding/calculation_error/misread_question/method_gap 之一, '
     '"error_confidence": 0到1的小数, "error_reason": 字符串}。\n'
     '题目：求 lim(x→0) sin(x)/x。学生答案：0。'),
]


def ollama_generate(prompt: str, force_json: bool, timeout: int = 60) -> tuple[str, int, str]:
    """直接打 Ollama /api/generate。返回 (文本, 耗时ms, 错误信息)。"""
    payload = {"model": MODEL, "prompt": prompt, "stream": False}
    if force_json:
        payload["format"] = "json"
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA}/api/generate", data=data,
        headers={"Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = json.loads(r.read().decode("utf-8"))
        return body.get("response", ""), int((time.time() - t0) * 1000), ""
    except Exception as e:
        return "", int((time.time() - t0) * 1000), str(e)


def layer_a() -> list[dict]:
    print("=" * 62)
    print(f"A 层：裸测 Ollama format=json（模型 {MODEL}）")
    print("=" * 62)
    out = []
    for name, prompt in A_PROMPTS:
        print(f"\n--- {name} ---")
        row = {"name": name}
        for force in (True, False):
            txt, ms, err = ollama_generate(prompt, force)
            tag = "format=json " if force else "无约束     "
            if err:
                print(f"  {tag} 请求失败: {err[:80]}")
                row["json" if force else "plain"] = {"ok": False, "err": err}
                continue
            obj, _ = ai.extract_json(txt)
            ok = obj is not None
            print(f"  {tag} {ms:>6}ms  解析={'成功' if ok else '失败'}  "
                  f"原文前 90 字: {txt[:90].replace(chr(10), ' / ')}")
            if ok:
                print(f"               → {json.dumps(obj, ensure_ascii=False)[:120]}")
            row["json" if force else "plain"] = {
                "ok": ok, "ms": ms, "raw": txt[:400],
                "obj": obj,
            }
        out.append(row)
    return out


def layer_b(limit: int) -> list[dict]:
    print("\n" + "=" * 62)
    print("B 层：走 call_ai 全流程（错题解析真实题目）")
    print("=" * 62)
    conn = get_conn()
    rows = conn.execute(
        "SELECT e.id, e.question_text, s.code AS subj_code "
        "FROM error_items e LEFT JOIN subjects s ON e.subject_id=s.id "
        "WHERE e.question_text IS NOT NULL AND TRIM(e.question_text) <> '' "
        "ORDER BY e.id LIMIT ?", (limit,)).fetchall()

    if not rows:
        print("库里没有可用题干，B 层跳过")
        return []

    results = []
    for r in rows:
        eid, q, code = r["id"], r["question_text"], r["subj_code"]
        subject = ai.subject_from_code(code)
        print(f"\n--- 错题 id={eid}  学科={subject}（库内 {code}）---")
        print(f"  题干: {q[:70]}")

        resp = ai.analyze_error(
            question=q,
            student_answer="（OCR 未识别出手写作答）",
            subject=subject,
        )
        flag = "通过" if resp.code == 0 else ("降级" if resp.code == 1 else "重试后仍不符")
        print(f"  code={resp.code}（{flag}） 耗时 {resp.elapsed_ms}ms "
              f"重试 {resp.retries} 次")
        if resp.code == 0 and resp.data:
            d = resp.data
            print(f"  error_type={d.get('error_type')} "
                  f"conf={d.get('error_confidence')} "
                  f"label={d.get('error_type_label')}")
            print(f"  reason={str(d.get('error_reason'))[:70]}")
            print(f"  solution={str(d.get('solution'))[:70]}")
        else:
            print(f"  data={resp.data}  raw 前 90 字: {str(resp.raw)[:90]}")
        results.append({
            "id": eid, "subject": subject, "code": resp.code,
            "retries": resp.retries, "ms": resp.elapsed_ms,
            "data": resp.data, "raw": str(resp.raw)[:300],
        })
    return results


def summarize(a: list[dict], b: list[dict]) -> dict:
    print("\n" + "=" * 62)
    print("汇总")
    print("=" * 62)
    a_json_ok = sum(1 for r in a if r.get("json", {}).get("ok"))
    a_plain_ok = sum(1 for r in a if r.get("plain", {}).get("ok"))
    print(f"A 层 format=json  : {a_json_ok}/{len(a)} 条能解析出 JSON")
    print(f"A 层 无约束对照   : {a_plain_ok}/{len(a)} 条能解析出 JSON")

    b_ok = sum(1 for r in b if r["code"] == 0)
    b_retry = sum(1 for r in b if r.get("retries", 0) > 0)
    print(f"\nB 层 全流程 code=0 : {b_ok}/{len(b)} 题")
    print(f"B 层 触发重试      : {b_retry}/{len(b)} 题")
    if b:
        valid = [r for r in b if r["code"] == 0 and isinstance(r["data"], dict)]
        confs = [r["data"].get("error_confidence") for r in valid]
        confs = [c for c in confs if isinstance(c, (int, float))]
        ge05 = sum(1 for c in confs if c >= 0.5)
        print(f"B 层 置信度 >=0.5  : {ge05}/{len(confs)} 题（可自动入库口径）")
        print(f"B 层 置信度取值    : {confs}")
        avg = sum(r["ms"] for r in b) / len(b)
        print(f"B 层 平均耗时      : {avg/1000:.1f} 秒/题")

    verdict = ("format=json 有效，模型能稳定产出合法 JSON"
               if a_json_ok == len(a) and len(a) > 0
               else "format=json 不足以让该模型稳定产出合法 JSON")
    print(f"\n结论：{verdict}")
    return {
        "model": MODEL, "a_json_ok": a_json_ok, "a_plain_ok": a_plain_ok,
        "a_total": len(a), "b_ok": b_ok, "b_total": len(b),
        "b_retry": b_retry, "verdict": verdict,
    }


def main() -> int:
    n = 7
    if "--n" in sys.argv:
        try:
            n = int(sys.argv[sys.argv.index("--n") + 1])
        except (IndexError, ValueError):
            pass

    if not ai.check_ollama_alive():
        print(f"Ollama 不在线（{OLLAMA}），先跑 ollama serve")
        return 1

    a = layer_a()
    b = layer_b(n)
    s = summarize(a, b)
    s["a_detail"] = a
    s["b_detail"] = b
    s["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")

    out = Path(__file__).resolve().parents[2] / "docs" / "真机联调记录.md"
    with out.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## {s['ts']} 真机联调（模型 {MODEL}）\n\n")
        f.write(f"- A 层裸测：format=json {s['a_json_ok']}/{s['a_total']} 条解析成功，"
                f"无约束对照 {s['a_plain_ok']}/{s['a_total']} 条\n")
        f.write(f"- B 层全流程：code=0 {s['b_ok']}/{s['b_total']} 题，"
                f"触发重试 {s['b_retry']} 题\n")
        f.write(f"- 结论：{s['verdict']}\n")
    print(f"\n已追加到 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
