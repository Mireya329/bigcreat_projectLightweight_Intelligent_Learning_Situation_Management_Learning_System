# -*- coding: utf-8 -*-
"""种子数据：开发联调用（演示用的 1 条错题）。

⚠️ 与 ocr_to_db.py 的关系：两者共用同一个 test 用户。
默认**不会**清库，避免误跑把 OCR 入库的真题删掉。
要重建演示数据请显式加参数：
    python src/backend/db/seed.py --reset
重建后需再跑一次 ocr_to_db.py 才能恢复真题：
    python src/backend/ocr_to_db.py
"""
import sys
from pathlib import Path
from db import get_conn, DB_PATH

ROOT = Path(__file__).resolve().parents[3]
OCR_FILE = ROOT / "data" / "ocr_fast_test" / "all_text.txt"


def main(reset=False):
    conn = get_conn()
    cur = conn.cursor()

    # 0. 只有显式 --reset 才清空（外键级联会带走 subjects / error_items / knowledge_tags）
    if reset:
        cur.execute("DELETE FROM users")
        print("[reset] 已清空 test 用户及其所有关联数据")
    elif cur.execute(
            "SELECT COUNT(*) FROM users WHERE username='test'").fetchone()[0]:
        print("[跳过] test 用户已存在，未做任何改动")
        print("       如需重建演示数据：python src/backend/db/seed.py --reset")
        print("       重建后记得重跑：python src/backend/ocr_to_db.py")
        conn.close()
        return

    # 1. 用户（? 是占位符，值放第二个参数的元组里）
    cur.execute(
        "INSERT INTO users (username, password_hash, nickname, education_stage, enrollment_year)"
        " VALUES (?,?,?,?,?)",
        ("test", "dev_plaintext", "测试同学", "university", 2025),
    )
    user_id = cur.lastrowid          # 拿到刚插入那行的 id

    # 2. 学科（多条用 executemany，一次插完）
    cur.executemany(
        "INSERT INTO subjects (user_id, name, code) VALUES (?,?,?)",
        [(user_id, "考研数学", "POSTGRAD_MATH"),
         (user_id, "英语四级", "CET4")],
    )

    # 3. 知识点
    cur.execute(
        "INSERT INTO knowledge_tags (name, subject, is_system, user_id)"
        " VALUES (?,?,?,?)",
        ("偏导数与高阶偏导数", "math", 1, user_id),
    )

    # 4. 取回"数学"这个学科的 id，给错题用
    row = cur.execute(
        "SELECT id FROM subjects WHERE user_id=? AND name=?", (user_id, "数学")
    ).fetchone()
    if row is None:                  # 查不到就别硬取 [0]，先报清楚
        print("出错：subjects 表里没有'数学'，实际内容：",
              cur.execute("SELECT id, user_id, name FROM subjects").fetchall())
        conn.close()
        return
    math_id = row[0]

    # 5. 错题：OCR 全文直接塞进 ocr_text
    ocr_text = OCR_FILE.read_text(encoding="utf-8")
    cur.execute(
        "INSERT INTO error_items (user_id, subject_id, ocr_text, question_text, mastery_level, source)"
        " VALUES (?,?,?,?,?,?)",
        (user_id, math_id, ocr_text, "习题二 偏导数与高阶偏导数", 0, "第一次真实试卷测试"),
    )

    # 6. 给这条错题打上知识点标签（多对多表）
    error_id = cur.lastrowid
    tag_id = cur.execute(
        "SELECT id FROM knowledge_tags WHERE user_id=? AND name=?",
        (user_id, "偏导数与高阶偏导数"),
    ).fetchone()[0]
    cur.execute(
        "INSERT INTO error_item_tags (error_item_id, tag_id) VALUES (?,?)",
        (error_id, tag_id),
    )

    conn.commit()                    # 改数据必须 commit，否则不落盘

    # 7. 验收查询：JOIN 出学科名
    row = cur.execute(
        "SELECT e.id, s.name, length(e.ocr_text), substr(e.question_text,1,20)"
        " FROM error_items e JOIN subjects s ON e.subject_id = s.id"
    ).fetchone()
    print(f"错题 id={row[0]}  学科={row[1]}  ocr长度={row[2]}字符  题干={row[3]}")

    # 8. 统计一下各表
    for t in ["users", "subjects", "knowledge_tags", "error_items", "error_item_tags"]:
        n = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        print(f"  {t}: {n} 条")

    conn.close()
    print("seed 完成，数据库:", DB_PATH)


if __name__ == "__main__":
    main(reset=("--reset" in sys.argv))
