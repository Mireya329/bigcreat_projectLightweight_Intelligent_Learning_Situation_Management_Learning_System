# -*- coding: utf-8 -*-
"""人工补录导入：把 pending_manual.json 里已填好题干的题写进 error_items。

设计要点：
  1. **stem 为空的条目直接跳过** —— 空白题干入库等于制造脏数据
  2. 人工录入的题 ocr_quality 固定 100（人工确认过，质量最高），
     ai_model 记为 'manual'，便于和 OCR/AI 来源区分
  3. 可重复运行：按 section+no 去重，不会重复插入

用法：
    python src/backend/ocr_manual_import.py            # 先演练，只看不写
    python src/backend/ocr_manual_import.py --apply    # 确认后写入
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "db"))

from db import get_conn, DB_PATH          # noqa: E402

PENDING = (HERE.parent.parent / "data" / "ocr_fast_test" / "pending_manual.json")
USERNAME = "test"
SUBJECT = "数学"
TAG = "偏导数与高阶偏导数"


def main():
    apply_mode = "--apply" in sys.argv
    d = json.loads(PENDING.read_text(encoding="utf-8"))
    items = d["items"]

    conn = get_conn()
    cur = conn.cursor()
    user_id = cur.execute(
        "SELECT id FROM users WHERE username=?", (USERNAME,)).fetchone()[0]
    subject_id = cur.execute(
        "SELECT id FROM subjects WHERE user_id=? AND name=?",
        (user_id, SUBJECT)).fetchone()[0]
    tag_id = cur.execute(
        "SELECT id FROM knowledge_tags WHERE user_id=? AND name=?",
        (user_id, TAG)).fetchone()[0]

    ready, skipped = [], []
    for it in items:
        stem = (it.get("stem") or "").strip()
        (ready if stem else skipped).append(it)

    print(f"待补录 {len(items)} 道：可导入 {len(ready)} 道，"
          f"未填题干 {len(skipped)} 道\n")
    for it in ready:
        print(f"  ✅ {it['section']} 第{it['no']}题  {it['stem'][:40]}")
    for it in skipped:
        print(f"  ⬜ {it['section']} 第{it['no']}题  —— 未填题干，跳过")

    if not apply_mode:
        print(f"\n[演练模式] 加 --apply 写入")
        conn.close()
        return

    n = 0
    for it in ready:
        stem = it["stem"].strip()
        src = (f"人工补录｜{it['section']} 第{it['no']}题"
               f"（原 OCR 行 {it['lines']}）")
        # 去重：同一章节同一题号只插一次
        exist = cur.execute(
            "SELECT COUNT(*) FROM error_items WHERE user_id=? AND source LIKE ?",
            (user_id, f"人工补录｜{it['section']} 第{it['no']}题%")).fetchone()[0]
        if exist:
            print(f"  已存在，跳过：{it['section']} 第{it['no']}题")
            continue
        cur.execute(
            "INSERT INTO error_items"
            " (user_id, subject_id, question_text, ocr_text, source,"
            "  mastery_level, ocr_quality, ai_model)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (user_id, subject_id, stem, it.get("ocr_remain", ""), src,
             0, 100, "manual"))
        eid = cur.lastrowid
        cur.execute("INSERT INTO error_item_tags VALUES (?,?)", (eid, tag_id))
        cur.execute(
            "INSERT INTO review_schedules (error_item_id, scheduled_for)"
            " VALUES (?, datetime('now','localtime','+3 day'))", (eid,))
        n += 1
    conn.commit()

    total = cur.execute(
        "SELECT COUNT(*) FROM error_items WHERE user_id=?",
        (user_id,)).fetchone()[0]
    print(f"\n已导入 {n} 道，该用户错题总数 {total} 道")
    print("数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
