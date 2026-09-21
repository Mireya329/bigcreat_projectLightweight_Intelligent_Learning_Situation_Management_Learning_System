#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""五个场景真机全覆盖。

之前只真机验过 error_analysis，其余四个场景的结论都来自桩函数。
这份脚本把队长的五个 scene 全跑一遍，回答一个问题：
**到底是哪几个场景不能用，还是只有分类不能用。**

weak_diagnosis 用库里的真实学情数据，不是编的。

用法：
    venv\\Scripts\\python.exe src/backend/ai_scene_sweep.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))

import ai_interface as ai

ESSAY = ("Nowadays more and more student use smartphone in the classroom. "
         "It is a big problem for teacher. Some people think it should be "
         "banned, but other think it can help learning. In my opinion, I "
         "agree with the first view, because phone is very distract.")


def scene_qa() -> dict:
    print("\n--- qa（知识点问答）---")
    r = ai.knowledge_qa("什么是拉格朗日中值定理？一句话说清。", subject="math")
    ok = r.code == 0 and isinstance(r.data, dict) and r.data.get("answer")
    print(f"  code={r.code}  重试{r.retries}次  {r.elapsed_ms}ms")
    if ok:
        print(f"  answer: {str(r.data.get('answer'))[:90]}")
    else:
        print(f"  失败: {r.error or r.raw}")
    return {"scene": "qa", "ok": ok, "code": r.code, "ms": r.elapsed_ms,
            "preview": (r.data or {}).get("answer", "")[:120]}


def scene_weak_diagnosis() -> dict:
    print("\n--- weak_diagnosis（薄弱点诊断，喂库里真实学情）---")
    from db import get_conn
    from study_stats import collect_stats
    conn = get_conn()
    cur = conn.cursor()
    uid = cur.execute("SELECT id FROM users LIMIT 1").fetchone()["id"]
    stats = collect_stats(cur, uid)

    # 只挑诊断用得上的几项，别把整包都塞进 prompt
    brief = {
        "错题数": stats["error"].get("total"),
        "错误类型分布": stats["error"].get("by_error_type"),
        "待复习": stats["error"].get("due_review"),
        "单词情况": stats["word"],
    }
    print(f"  输入摘要: {json.dumps(brief, ensure_ascii=False)[:110]}")

    r = ai.diagnose_weakness(stats=brief, subject="math")
    ok = r.code == 0 and isinstance(r.data, dict) and r.data.get("weak_points")
    print(f"  code={r.code}  重试{r.retries}次  {r.elapsed_ms}ms")
    if ok:
        wp = r.data.get("weak_points")
        print(f"  weak_points: {str(wp)[:90]}")
        print(f"  advice: {str(r.data.get('advice'))[:90]}")
    else:
        print(f"  失败: {r.error or r.raw}")
    return {"scene": "weak_diagnosis", "ok": ok, "code": r.code,
            "ms": r.elapsed_ms, "preview": str((r.data or {}).get("advice", ""))[:120]}


def scene_school_recommend() -> dict:
    print("\n--- school_recommend（择校，本就该是空的）---")
    r = ai.school_recommend(profile={"本科": "某二本", "专业": "计算机",
                                     "目标分": 340, "意向地区": "江苏"})
    ok = r.code == 0 and isinstance(r.data, dict)
    print(f"  code={r.code}  重试{r.retries}次  {r.elapsed_ms}ms")
    if ok:
        d = r.data
        print(f"  reach={d.get('reach')} match={d.get('match')} "
              f"safety={d.get('safety')}")
        print(f"  note: {str(d.get('note'))[:90]}")
    else:
        print(f"  失败: {r.error or r.raw}")
    return {"scene": "school_recommend", "ok": ok, "code": r.code,
            "ms": r.elapsed_ms,
            "preview": str((r.data or {}).get("note", ""))[:120]}


def scene_essay_review() -> dict:
    print("\n--- essay_review（英语作文批改）---")
    r = ai.review_essay(ESSAY, subject="english")
    ok = r.code == 0 and isinstance(r.data, dict) and r.data.get("corrected")
    print(f"  code={r.code}  重试{r.retries}次  {r.elapsed_ms}ms")
    if ok:
        print(f"  issues: {str(r.data.get('issues'))[:90]}")
        print(f"  corrected: {str(r.data.get('corrected'))[:90]}")
    else:
        print(f"  失败: {r.error or r.raw}")
    return {"scene": "essay_review", "ok": ok, "code": r.code,
            "ms": r.elapsed_ms,
            "preview": str((r.data or {}).get("corrected", ""))[:120]}


def scene_judge_fragment() -> dict:
    print("\n--- judge_fragment（自己扩展的 OCR 漏题兜底）---")
    cases = [
        ("上一题的延续", "则 f(x) 在 x=0 处", "2. 设 z = arctan",
         "= lim(h→0) [f(h)-f(0)]/h = 1"),
        ("独立的新题", "求 f(x) 的极值。", "3. 设 f(x,y)=ln|x+y|",
         "1. 已知 z=x^4+y^4−4x^2y^2，求 zx(1,2)"),
        ("残缺看不清", "（上一题末尾）", "（下一题开头）", "Ox100001 f(xbsy6"),
    ]
    results = []
    for name, prev, nxt, frag in cases:
        r = ai.judge_fragment(fragment=frag, prev=prev, next_=nxt)
        ch = (r.data or {}).get("choice")
        print(f"  [{name}] code={r.code} choice={ch} "
              f"({r.elapsed_ms}ms, 重试{r.retries}次)")
        results.append({"case": name, "code": r.code, "choice": ch,
                        "ms": r.elapsed_ms})
    ok = all(x["code"] == 0 for x in results)
    return {"scene": "judge_fragment", "ok": ok, "code": 0 if ok else 1,
            "ms": sum(x["ms"] for x in results), "detail": results}


def main() -> int:
    print("=" * 62)
    print(f"五场景真机覆盖（模型 {ai.MODEL_NAME}）")
    print("=" * 62)

    if not ai.check_ollama_alive():
        print(f"Ollama 不在线（{ai.OLLAMA_BASE_URL}），先跑 ollama serve")
        return 1

    t0 = time.time()
    rows = [
        scene_qa(),
        scene_weak_diagnosis(),
        scene_school_recommend(),
        scene_essay_review(),
        scene_judge_fragment(),
    ]

    print("\n" + "=" * 62)
    print("汇总")
    print("=" * 62)
    for r in rows:
        mark = "✓" if r["ok"] else "✗"
        print(f"  {mark} {r['scene']:<18} code={r['code']}  {r['ms']/1000:.1f}s")
    passed = sum(1 for r in rows if r["ok"])
    print(f"\n通过 {passed}/{len(rows)} 个场景，总耗时 {(time.time()-t0)/60:.1f} 分钟")

    out = Path(__file__).resolve().parents[2] / "docs" / "真机联调记录.md"
    with out.open("a", encoding="utf-8") as f:
        f.write(f"\n\n## {time.strftime('%Y-%m-%d %H:%M:%S')} 五场景覆盖"
                f"({ai.MODEL_NAME})\n\n")
        for r in rows:
            f.write(f"- {'通过' if r['ok'] else '未通过'} `{r['scene']}` "
                    f"code={r['code']} {r['ms']/1000:.1f}s\n")
        f.write(f"\n通过 {passed}/{len(rows)} 个场景\n")
    print(f"已追加到 {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
