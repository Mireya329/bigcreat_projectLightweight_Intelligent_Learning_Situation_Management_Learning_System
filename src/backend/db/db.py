# -*- coding: utf-8 -*-
"""统一数据库连接层：全项目唯一的连库入口，禁止其他文件自己 sqlite3.connect"""
from pathlib import Path
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
DB_PATH = ROOT / "data" / "learning.db"


def get_conn():
    """返回一个已开外键、可用列名取值的数据库连接"""
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")   # 外键必须每个连接单独开！
    conn.row_factory = sqlite3.Row             # 让结果可以用 row["列名"] 取值
    return conn


# 自测：python db.py 直接跑这里
if __name__ == "__main__":
    conn = get_conn()
    print("连接成功:", DB_PATH)
    print("外键开关:", conn.execute("PRAGMA foreign_keys").fetchone()[0])
    print("自增测试: SELECT 1 =", conn.execute("SELECT 1").fetchone()[0])
    print("现有表数量:", len(conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()))
    conn.close()
    print("db.py 自测通过")
