# -*- coding: utf-8 -*-
"""把错题和易错词自动转成 Anki 卡片（错词/错题 → 记忆系统 的联动）。

统筹文档里写"打通单词训练、间隔重复记忆、AI 错题识别数据链路，
实现错词、错题、考点数据双向流转"——本脚本就是这条链路的入口：
没有它，Anki 卡片表是空的，复习页没东西可复习。

规则：
  - 错题卡：正面 = 题干（截前 120 字），背面 = AI 解析（截前 400 字）
    **只收 ocr_quality >= 70 的题**（低分题题干残缺，做成卡片是害人）
  - 单词卡：正面 = 单词，背面 = 释义（trans 是 JSON 字符串，要 parse 后拼成一行）
    只收累计答错 >= 1 次的词（"错词"才值得做成卡片）

幂等：按 error_item_id / word_id 去重，已存在的跳过，可重复运行。

用法：
    python src/backend/anki_sync.py            # 演练，只看不写
    python src/backend/anki_sync.py --apply    # 写入
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn, DB_PATH          # noqa: E402

USERNAME = "test"
MIN_OCR_QUALITY = 70


def main():
    apply_mode = "--apply" in sys.argv
    conn = get_conn()
    cur = conn.cursor()
    uid = cur.execute(
        "SELECT id FROM users WHERE username=?", (USERNAME,)).fetchone()[0]

    added_err = added_word = skipped = 0

    # ---------- 1. 错题 → 卡片 ----------
    rows = cur.execute(
        "SELECT id, question_text, analysis, ocr_quality FROM error_items"
        " WHERE user_id=? AND ocr_quality >= ? ORDER BY id",
        (uid, MIN_OCR_QUALITY)).fetchall()
    print(f"合格错题 {len(rows)} 道（ocr_quality >= {MIN_OCR_QUALITY}）")

    for r in rows:
        exist = cur.execute(
            "SELECT COUNT(*) FROM anki_cards WHERE user_id=? AND error_item_id=?",
            (uid, r["id"])).fetchone()[0]
        if exist:
            skipped += 1
            continue
        front = (r["question_text"] or "").strip()[:120]
        back = (r["analysis"] or "").strip()[:400] or "（暂无解析）"
        if not front:
            continue
        if apply_mode:
            cur.execute(
                "INSERT INTO anki_cards (user_id, error_item_id, front, back)"
                " VALUES (?,?,?,?)", (uid, r["id"], front, back))
        added_err += 1

    # ---------- 2. 易错词 → 卡片 ----------
    wrows = cur.execute(
        "SELECT w.id, w.word, w.trans, SUM(r.wrong_count) wrong"
        " FROM word_records r JOIN words w ON r.word_id=w.id"
        " WHERE r.user_id=? GROUP BY w.id HAVING wrong >= 1"
        " ORDER BY wrong DESC LIMIT 50", (uid,)).fetchall()
    print(f"易错词 {len(wrows)} 个（答错 >= 1 次）")

    for r in wrows:
        exist = cur.execute(
            "SELECT COUNT(*) FROM anki_cards WHERE user_id=? AND word_id=?",
            (uid, r["id"])).fetchone()[0]
        if exist:
            skipped += 1
            continue
        try:                                   # trans 是 JSON 字符串
            trans = json.loads(r["trans"] or "[]")
            back = "；".join(trans) if isinstance(trans, list) else str(trans)
        except Exception:
            back = r["trans"] or ""
        if apply_mode:
            cur.execute(
                "INSERT INTO anki_cards (user_id, word_id, front, back)"
                " VALUES (?,?,?,?)", (uid, r["id"], r["word"], back[:300]))
        added_word += 1

    if apply_mode:
        conn.commit()

    print(f"\n本次可新增：错题卡 {added_err} 张、单词卡 {added_word} 张"
          f"（已存在跳过 {skipped} 张）")
    if not apply_mode:
        print("[演练模式] 加 --apply 写入")

    n = cur.execute("SELECT COUNT(*) FROM anki_cards WHERE user_id=?",
                    (uid,)).fetchone()[0]
    due = cur.execute(
        "SELECT COUNT(*) FROM anki_cards WHERE user_id=?"
        " AND due_date <= date('now','localtime')", (uid,)).fetchone()[0]
    print(f"卡片总数 {n} 张，今日到期 {due} 张")
    print("数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
