# -*- coding: utf-8 -*-
"""漏题找回：把切分算法漏掉的题目尽力补回来。

先做"可救性"判断，分三类处理：
  1. 信息已丢失（OCR 碎片 < 20 字符）→ 标记需人工补录，**不浪费 AI**
     （对着 "B.C" "8ふ" 这种碎片推理，只会得到幻觉，见条目 18 的教训）
  2. 文本足够 → 交给 AI 判断：A 上一题延续 / B 独立新题 / C 无法判断
  3. 判为 B 的，加 --apply 才真正写入 error_items（source 标注 AI 补录）

用法：
    python src/backend/ocr_recover.py            # 只报告，不动数据库
    python src/backend/ocr_recover.py --apply    # 确认无误后再写入
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "db"))

import ai_interface                                    # noqa: E402
from ocr_split_v2 import split_questions, SRC          # noqa: E402
from db import get_conn                                # noqa: E402
from ocr_quality import score_text                     # noqa: E402

GOLDEN = SRC.parent / "golden_splits.json"
MIN_TEXT = 20          # 低于这个字符数判定为"信息已丢失"


def main():
    apply_mode = "--apply" in sys.argv
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    golden = g["questions"]
    raw = SRC.read_text(encoding="utf-8").splitlines()
    pred = split_questions(SRC.read_text(encoding="utf-8"))

    # 找出 golden 里没被切分命中的题
    missed = []
    for gi, gq in enumerate(golden):
        hit = any(gq["start_line"] <= p["start_line"] <= gq["end_line"]
                  for p in pred)
        if not hit:
            missed.append((gi, gq))

    print(f"golden {len(golden)} 道，漏切 {len(missed)} 道\n")

    if not missed:
        print("没有漏题，切分召回率 100%")
        return

    # 实测警告（2026-09-21，0.5b）：judge_fragment 判定基本是随机的，
    # 3 个用例各跑 3 次，3/3 都不一致。AI 判 B 会真的写进 error_items，
    # 判定不可靠时补录等于往库里灌不存在的题。
    if apply_mode and not ai_interface.STABILITY_CHECK:
        print("⚠️  警告：judge_fragment 在当前模型上判定不稳定")
        print("   （实测 3 个用例 × 3 次，3/3 结果不一致）")
        print("   AI 判为 B 的会被写进 error_items，可能补出不存在的题。")
        print("   建议先在 ai_interface 里把 STABILITY_CHECK 置 True 再 --apply，")
        print("   或等 7b 到位后重跑。\n")

    results = []
    for gi, gq in missed:
        seg = raw[gq["start_line"] - 1: gq["end_line"]]
        text = "\n".join(l for l in seg if l.strip())
        print("=" * 58)
        print(f"漏题：{gq['section']} 第{gq['no']}题  行{gq['start_line']}-{gq['end_line']}")
        print(f"OCR 文本 {len(text)} 字符")

        # ---- 1. 可救性判断 ----
        if len(text.strip()) < MIN_TEXT:
            print(f"  ⚫ 信息已丢失（不足 {MIN_TEXT} 字符）：{text[:40]!r}")
            print("     判定：需人工对照原图补录，不送 AI（避免幻觉）")
            results.append(("manual", gq, None))
            print()
            continue

        # ---- 2. 送 AI 判断归属 ----
        prev_q = golden[gi - 1] if gi > 0 else None
        next_q = golden[gi + 1] if gi + 1 < len(golden) else None
        prev = "\n".join(raw[prev_q["end_line"] - 2: prev_q["end_line"]]) if prev_q else ""
        nxt = "\n".join(raw[next_q["start_line"] - 1: next_q["start_line"] + 2]) if next_q else ""

        if not ai_interface.check_ollama_alive():
            print("  ⚠️ Ollama 离线，跳过 AI 判断")
            results.append(("offline", gq, None))
            continue

        resp = ai_interface.judge_fragment(fragment=text, prev=prev, next_=nxt)
        ans = resp.content.strip()

        # 严格校验输出格式：必须以 A/B/C 开头（可跟标点），且不能太长。
        # 小模型（0.5b）经常不按格式来，会复述 prompt 或自说自话——
        # 只搜关键词会误判（实测踩过：模型背了一遍选项，被当成了"B 新题"）。
        m = re.match(r"^\s*([ABC])\s*[\.、。:：]?\s", ans)
        if not m or len(ans) > 400:
            verdict = "unknown"
            print("  ⚠️ 输出不符合约定格式（未以 A/B/C 开头或过长），视为无法判断")
        else:
            verdict = {"A": "continuation", "B": "new_question",
                       "C": "unknown"}[m.group(1)]
        print(f"  AI 判定：{verdict}（{resp.elapsed_ms/1000:.1f} 秒）")
        print("  AI 原文：", ans[:300].replace("\n", " "))
        results.append((verdict, gq, text))
        print()

    # ---- 3. 汇总 ----
    print("=" * 58)
    print("漏题找回汇总")
    for v, gq, txt in results:
        label = {"manual": "需人工补录", "new_question": "AI 判定为独立新题",
                 "continuation": "AI 判定为上一题延续", "unknown": "AI 无法判断",
                 "offline": "Ollama 离线未判断"}[v]
        print(f"  {gq['section']} 第{gq['no']}题（行{gq['start_line']}-{gq['end_line']}）: {label}")

    n_new = [r for r in results if r[0] == "new_question"]
    if not apply_mode:
        print(f"\n[演练模式] 有 {len(n_new)} 道可入库，加 --apply 才真正写入")
        return

    # ---- 4. 写入 ----
    conn = get_conn()
    cur = conn.cursor()
    user_id = cur.execute(
        "SELECT id FROM users WHERE username='test'").fetchone()[0]
    subject_id = cur.execute(
        "SELECT id FROM subjects WHERE user_id=? AND name='数学'",
        (user_id,)).fetchone()[0]
    for _, gq, txt in n_new:
        q, _ = score_text(txt)
        cur.execute(
            "INSERT INTO error_items"
            " (user_id, subject_id, question_text, ocr_text, source,"
            "  mastery_level, ocr_quality)"
            " VALUES (?,?,?,?,?,?,?)",
            (user_id, subject_id, txt, txt,
             f"AI 补录｜{gq['section']} 第{gq['no']}题（行{gq['start_line']}-{gq['end_line']}）",
             0, q))
        conn.commit()
        print(f"  已入库：{gq['section']} 第{gq['no']}题")
    conn.close()


if __name__ == "__main__":
    main()
