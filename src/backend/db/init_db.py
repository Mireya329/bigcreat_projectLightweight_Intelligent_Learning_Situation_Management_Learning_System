
from pathlib import Path

ROOT=Path(__file__).resolve().parents[3]
DB_PATH=ROOT/"data"/"learning.db"
SCHEMA_PATH=Path(__file__).resolve().parent/"schema.sql"

print("项目根目录：",ROOT)
print("数据库路径：",DB_PATH)
print("脚本路径：",SCHEMA_PATH)

import sqlite3

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = ON")

schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")
conn.executescript(schema_sql)

rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
print("数据库中的表：")
for r in rows:
    print("  -", r[0])

conn.close()
print("建库完成:", DB_PATH)
