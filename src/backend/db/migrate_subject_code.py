# -*- coding: utf-8 -*-
"""迁移：给 subjects 表增加 code 字段，并初始化五大科目。

背景：3 号（前端）在《前端冗余模块删减清单》里问"五大科目的后端字段标识是什么，
前端筛选需要匹配"。本脚本落实该约定：

    code（英文，稳定，前端筛选用）    name（中文，显示用）
    POSTGRAD_MATH        考研数学
    POSTGRAD_ENGLISH     考研英语
    POSTGRAD_POLITICS    考研政治
    CET4                 英语四级
    CET6                 英语六级

迁移策略：
  - 已有的"数学"改名为"考研数学"并赋 code（11 道错题跟着走，不丢数据）
  - 已有的"英语"改名为"英语四级"并赋 code（词库 CET4_T 归属此科）
  - 补齐其余三个科目

幂等：重复运行不会重复插入。

用法：python src/backend/db/migrate_subject_code.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_conn, DB_PATH          # noqa: E402

FIVE_SUBJECTS = [
    ("POSTGRAD_MATH", "考研数学"),
    ("POSTGRAD_ENGLISH", "考研英语"),
    ("POSTGRAD_POLITICS", "考研政治"),
    ("CET4", "英语四级"),
    ("CET6", "英语六级"),
]

# 旧名 -> (新名, code)
RENAME = {
    "数学": ("考研数学", "POSTGRAD_MATH"),
    "英语": ("英语四级", "CET4"),
}


def ensure_column(conn):
    """老库没有 code 字段时自动加（幂等）"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(subjects)")]
    if "code" not in cols:
        conn.execute("ALTER TABLE subjects ADD COLUMN code TEXT")
        conn.commit()
        print("[迁移] subjects 已增加 code 字段")
    return "code" in [r["name"] for r in
                      conn.execute("PRAGMA table_info(subjects)")]


def main():
    conn = get_conn()
    cur = conn.cursor()
    ensure_column(conn)

    uid = cur.execute(
        "SELECT id FROM users WHERE username='test'").fetchone()[0]

    # 1. 已有学科改名归位（错题通过 subject_id 外键跟着走，不丢数据）
    for old, (new, code) in RENAME.items():
        cur.execute(
            "UPDATE subjects SET name=?, code=? WHERE user_id=? AND name=?",
            (new, code, uid, old))

    # 2. 补齐缺失的科目
    added = []
    for code, name in FIVE_SUBJECTS:
        n = cur.execute(
            "SELECT COUNT(*) FROM subjects WHERE user_id=? AND code=?",
            (uid, code)).fetchone()[0]
        if not n:
            cur.execute(
                "INSERT INTO subjects (user_id, name, code) VALUES (?,?,?)",
                (uid, name, code))
            added.append(f"{code} {name}")
    conn.commit()

    print("当前科目：")
    for r in cur.execute(
            "SELECT s.id, s.code, s.name,"
            " (SELECT COUNT(*) FROM error_items WHERE subject_id=s.id) n"
            " FROM subjects s WHERE s.user_id=? ORDER BY s.id", (uid,)):
        print(f"  {str(r['code']):<18} {r['name']:<6} 错题 {r['n']} 道")

    if added:
        print("本次新增:", "、".join(added))
    print("数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
