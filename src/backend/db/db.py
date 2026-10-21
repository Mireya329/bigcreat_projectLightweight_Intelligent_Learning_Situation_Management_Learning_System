# -*- coding: utf-8 -*-
"""统一数据库连接层：全项目唯一的连库入口，禁止其他文件自己 sqlite3.connect

2026-10-21 按队长《给二号_Q01小批量试导交接要求》第四节 + 《对二号第五轮反馈的
组长回复》第六节改造，解决"以为在写测试库、实际在写别的库"的问题：

  · **单一配置**：解析优先级固定为 命令行 --db  >  环境变量 LEARNING_DB_PATH
    >  默认 data/learning.db，三处来源只有一个函数决定（resolve_db_path）。
    以前各脚本各写各的，controlled_branch_test.py 甚至前段读环境变量、后段
    又把它改回固定路径，等于两套配置。
  · **写入前必须打印绝对路径**：announce_db() 会打印 resolve 后的绝对路径、
    文件是否存在、大小，并区分"服务端/客户端"。环境变量只影响**当前进程**，
    改环境变量不会影响已经启动的服务进程——这条要写在日志里让人看见。
  · **服务端数据库明确**：api.py 启动时调用 announce_db(role="服务端")，
    /health 返回绝对路径，联调时先核对这一行再动手。

用法：
    from db import get_conn, DB_PATH, announce_db
    conn = get_conn()                      # 用当前配置
    announce_db(role="导入脚本")            # 任何写库之前打印一次
"""
from pathlib import Path
import os
import sqlite3

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DB = ROOT / "data" / "learning.db"

# 命令行 --db 通过 set_cli_db() 注入；留空表示没给，走环境变量/默认
_CLI_DB: Path | None = None


def set_cli_db(path: str | os.PathLike | None) -> None:
    """命令行 --db 入口。传 None/空串表示清空覆盖。"""
    global _CLI_DB
    _CLI_DB = Path(path).expanduser() if path else None


def resolve_db_path(cli: str | os.PathLike | None = None) -> Path:
    """唯一的路径解析：--db > LEARNING_DB_PATH > 默认正式库。永远返回绝对路径。"""
    raw = cli if cli else _CLI_DB
    if raw:
        return Path(raw).expanduser().resolve()
    env = os.environ.get("LEARNING_DB_PATH")
    if env:
        return Path(env).expanduser().resolve()
    return DEFAULT_DB.resolve()


def which_source(cli: str | os.PathLike | None = None) -> str:
    """当前生效的是哪一路配置——打印出来才知道自己连的是谁。"""
    raw = cli if cli else _CLI_DB
    if raw:
        return "--db 命令行参数"
    if os.environ.get("LEARNING_DB_PATH"):
        return "环境变量 LEARNING_DB_PATH"
    return "默认（正式库）"


def describe_db(path: Path | None = None) -> str:
    """一行说明：绝对路径 + 来源 + 是否存在 + 大小。"""
    p = (path or resolve_db_path()).resolve()
    if p.exists():
        size = p.stat().st_size
        tail = f"存在，{size / 1024:.1f} KB"
    else:
        tail = "不存在（本次将新建）"
    return f"{p}  [{which_source()}]  {tail}"


def announce_db(path: Path | None = None, role: str = "写入方") -> Path:
    """**任何写库动作之前**调用：打印并核对最终绝对路径。"""
    p = (path or resolve_db_path()).resolve()
    print(f"[{role}] 数据库来源：{which_source()}")
    print(f"[{role}] 实际绝对路径：{p}")
    print(f"[{role}] 状态：{'已存在' if p.exists() else '不存在，将新建'}", end="")
    if p.exists():
        print(f"，{p.stat().st_size / 1024:.1f} KB")
    else:
        print()
    return p


DB_PATH = resolve_db_path()


def get_conn(path: str | os.PathLike | None = None):
    """返回一个已开外键、可用列名取值的数据库连接

    传 path 时连指定库（测试用），不传则用 resolve_db_path() 的当前配置。
    """
    target = Path(path).expanduser().resolve() if path else resolve_db_path()
    conn = sqlite3.connect(target)
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
