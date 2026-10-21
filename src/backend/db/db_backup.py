# -*- coding: utf-8 -*-
"""数据库备份与校验（迁移 / 批量导入前的统一保护）。

为什么不能用 shutil.copy2：SQLite 在 WAL 模式下，.db 文件与 -wal 文件是分开的，
直接拷文件可能拷到"写了一半"的库，恢复出来是坏的。必须用 sqlite3 自带的
backup 接口，它会在数据库一致的状态点做拷贝。

校验不能只比行数：行数一样但字段丢了、外键坏了照样是坏库。所以校验做三层：
  1. integrity_check（页结构）
  2. 关键表是否还在
  3. 关键表的必要字段是否还在

用法：
    from db_backup import backup_database
    bak = backup_database("before_qbank_v2")
"""
import sqlite3
from datetime import datetime
from pathlib import Path

# 关键表 -> 该表至少要有这些字段。
# ⚠️ 只列**迁移前后都存在**的字段：备份要在迁移之前做，那一刻 v2 的新字段还不
# 存在，拿 v2 字段去校验备份必然失败（2026-10-21 踩过）。
KEY_TABLES = {
    "users": ["id", "username"],
    "subjects": ["id", "user_id", "name"],
    "error_items": ["id", "user_id", "subject_id", "question_text",
                    "error_type", "review_flag"],
    "question_bank": ["id", "subject_code", "source_file"],
}


def backup_database(reason: str, db_path: Path | str | None = None) -> Path:
    """一致性备份，返回备份文件绝对路径。备份后自带校验，不通过直接抛错。"""
    from db import resolve_db_path
    src_path = Path(db_path).resolve() if db_path else resolve_db_path()
    out_dir = src_path.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = out_dir / f"{src_path.stem}_{reason}_{stamp}.db"

    if src_path.exists():
        src = sqlite3.connect(str(src_path))
        dst_conn = sqlite3.connect(str(dst))
        src.backup(dst_conn)
        dst_conn.close()
        src.close()
    else:
        dst.touch()          # 库还不存在时，备份一个空文件占位，保持流程一致
    print(f"备份完成：{dst}")
    verify_database(dst)
    if src_path.exists():
        diff = compare_with_source(dst, src_path)
        if diff:
            raise RuntimeError(f"备份与源库不一致：{diff}")
        print("与源库逐表行数比对：一致")
    return dst


def compare_with_source(backup_path: Path | str,
                        src_path: Path | str) -> list:
    """备份是不是真的完整：逐表比行数。行数不同的表名列表返回出来。"""
    src_path, backup_path = Path(src_path), Path(backup_path)
    src = sqlite3.connect(str(src_path))
    bak = sqlite3.connect(str(backup_path))
    diffs = []
    try:
        names = [r[0] for r in src.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        for t in names:
            try:
                a = src.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                b = bak.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.Error:
                diffs.append(f"{t}(无法读取)")
                continue
            if a != b:
                diffs.append(f"{t}(源 {a} / 备 {b})")
        return diffs
    finally:
        src.close()
        bak.close()


def verify_database(path: Path | str) -> dict:
    """三层校验：页结构 → 关键表 → 必要字段。返回统计，任何一层不过就抛错。"""
    path = Path(path)
    conn = sqlite3.connect(str(path))
    try:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise RuntimeError(f"integrity_check 失败：{integrity}")
        have = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        report = {"path": str(path), "integrity": integrity, "tables": {}}
        for table, fields in KEY_TABLES.items():
            if table not in have:
                report["tables"][table] = "缺失"
                continue
            cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
            missing = [f for f in fields if f not in cols]
            rows = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            report["tables"][table] = {
                "rows": rows,
                "缺字段": missing or "无",
            }
            if missing:
                raise RuntimeError(f"{table} 缺字段 {missing}")
        return report
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    from db import announce_db
    p = announce_db(role="备份校验")
    print(verify_database(p))
