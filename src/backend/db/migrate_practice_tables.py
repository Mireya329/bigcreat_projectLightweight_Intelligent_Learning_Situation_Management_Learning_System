# -*- coding: utf-8 -*-
"""迁移：为趋势图和时长图补建两张表（quiz_records / study_sessions）。

设计说明：**迁移统一执行 schema.sql，不在本文件里重复写 SQL**。
因为 schema.sql 里所有建表都是 `CREATE TABLE IF NOT EXISTS`，
重复执行不会动已有数据，也不会重复建表——这样 SQL 只有一份，不会两边漂移。

用法：python src/backend/db/migrate_practice_tables.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_conn, DB_PATH          # noqa: E402

SCHEMA = Path(__file__).resolve().parent / "schema.sql"


def main():
    conn = get_conn()
    before = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}

    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    conn.commit()

    after = {r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    added = sorted(after - before)

    if added:
        print("本次新增表:", "、".join(added))
    else:
        print("没有新表要建（已全部存在）")

    for t in ("quiz_records", "study_sessions"):
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cols = [r["name"] for r in conn.execute(f"PRAGMA table_info({t})")]
        print(f"  {t}: {n} 条，字段 {cols}")
    print("数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
