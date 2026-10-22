# -*- coding: utf-8 -*-
"""从 schema.sql 全新建立数据库（新机器、没有旧库时用）。

⚠️ 旧库升级不要跑这个，走对应的 migrate_*.py 迁移脚本。

2026-10-22 改：原来这里用 ``Path(__file__).parents[3]`` 硬算根目录、
``sqlite3.connect(DB_PATH)`` 直连 data/learning.db，**绕过了 db.py 的单一配置**。
后果有两个，都是队长第五轮回复第六节点名要防的那类：

  1. 在本机做"干净新装"实验时无法指向临时库，只能往正式库写；
  2. 别的成员设了 LEARNING_DB_PATH 也照样打到默认库，写错库不报错。

现在统一走 ``db.resolve_db_path()``（优先级 --db > LEARNING_DB_PATH > 默认），
并在建表前 ``announce_db()`` 打印出最终绝对路径——先看清楚再写。

用法：
  venv/Scripts/python.exe src/backend/db/init_db.py                 # 默认正式库
  venv/Scripts/python.exe src/backend/db/init_db.py --db <绝对路径>  # 指定库（推荐）
  LEARNING_DB_PATH=<绝对路径> venv/Scripts/python.exe src/backend/db/init_db.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sqlite3                                          # noqa: E402

import db                                               # noqa: E402
from db import announce_db, get_conn, resolve_db_path  # noqa: E402

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def _cli_arg(name: str) -> str | None:
    """从命令行里取 --xxx 的值，支持 `--db=路径` 和 `--db 路径` 两种写法。"""
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == f"--{name}" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(f"--{name}="):
            return a.split("=", 1)[1]
    return None


db.set_cli_db(_cli_arg("db"))
DB_PATH = announce_db(role="初始化")

print("schema 文件：", SCHEMA_PATH)
if not SCHEMA_PATH.is_file():
    raise SystemExit(f"找不到 schema 文件：{SCHEMA_PATH}")

DB_PATH.parent.mkdir(parents=True, exist_ok=True)

conn = sqlite3.connect(DB_PATH)
conn.execute("PRAGMA foreign_keys = ON")
conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))

rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                    " AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
print(f"\n建表完成，业务表 {len(rows)} 张：")
for i, (n,) in enumerate(rows, 1):
    cnt = conn.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]
    print(f"  {i:2d}. {n:<28} {cnt} 行")
print("\n完整性检查：", conn.execute("PRAGMA integrity_check").fetchone()[0])
conn.close()
print("数据库路径：", DB_PATH)
