# -*- coding: utf-8 -*-
"""错题入库：把切分结果写入 error_items，并打标签、排复习计划。

用法：python src/backend/ocr_to_db.py
"""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                 # 为了 import ocr_split_v2
sys.path.insert(0, str(HERE / "db"))          # 为了 import db

from ocr_split_v2 import split_questions, SRC     # noqa: E402
from db import get_conn, DB_PATH                  # noqa: E402

USERNAME = "test"
SUBJECT = "数学"
TAG = "偏导数与高阶偏导数"
SOURCE = "习题二 偏导数与高阶偏导数（真实试卷 OCR）"


def main():
    conn = get_conn()
    cur = conn.cursor()

    # 0. 定位用户和学科（动态查 id，别写死）
    user_id = cur.execute(
        "SELECT id FROM users WHERE username=?", (USERNAME,)).fetchone()[0]
    subject_id = cur.execute(
        "SELECT id FROM subjects WHERE user_id=? AND name=?",
        (user_id, SUBJECT)).fetchone()[0]
    tag_id = cur.execute(
        "SELECT id FROM knowledge_tags WHERE user_id=? AND name=?",
        (user_id, TAG)).fetchone()[0] if cur.execute(
        "SELECT COUNT(*) FROM knowledge_tags WHERE user_id=? AND name=?",
        (user_id, TAG)).fetchone()[0] else None

    # 1. 清空这个用户的旧错题（外键级联会带走标签关联和复习计划）
    cur.execute("DELETE FROM error_items WHERE user_id=?", (user_id,))

    # 2. 切分 + 逐题入库
    qs = split_questions(SRC.read_text(encoding="utf-8"))
    print(f"切出 {len(qs)} 道，开始入库...\n")

    for q in qs:
        body = "\n".join(q["lines"])
        source = f"{SOURCE}｜{q['section']} 第{q['no']}题（行{q['start_line']}-{q['end_line']}）"
        cur.execute(
            "INSERT INTO error_items"
            " (user_id, subject_id, question_text, ocr_text, source, mastery_level)"
            " VALUES (?,?,?,?,?,?)",
            (user_id, subject_id, body, body, source, 0),
        )
        eid = cur.lastrowid

        if tag_id:
            cur.execute("INSERT INTO error_item_tags VALUES (?,?)", (eid, tag_id))

        # 艾宾浩斯：第一次复习排 3 天后
        cur.execute(
            "INSERT INTO review_schedules (error_item_id, scheduled_for)"
            " VALUES (?, datetime('now','localtime','+3 day'))", (eid,))

    conn.commit()

    # 3. 验收
    print("入库结果：")
    rows = cur.execute(
        "SELECT e.id, e.source, length(e.question_text), s.name"
        " FROM error_items e JOIN subjects s ON e.subject_id = s.id"
        " WHERE e.user_id=? ORDER BY e.id", (user_id,)).fetchall()
    for r in rows:
        src = r["source"].split("｜")[-1]
        print(f"  id={r['id']:<3} {r['name']}  {r[2]:>4}字符  {src}")

    n_tag = cur.execute("SELECT COUNT(*) FROM error_item_tags").fetchone()[0]
    n_rev = cur.execute("SELECT COUNT(*) FROM review_schedules").fetchone()[0]
    print(f"\n错题 {len(rows)} 条，标签关联 {n_tag} 条，复习计划 {n_rev} 条")
    print("数据库:", DB_PATH)

    conn.close()


if __name__ == "__main__":
    main()
