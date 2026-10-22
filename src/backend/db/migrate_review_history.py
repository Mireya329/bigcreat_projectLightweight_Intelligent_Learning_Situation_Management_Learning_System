# -*- coding: utf-8 -*-
"""迁移：新增 error_review_history（错题结论归档表）。

背景（队长 v2.2 第四节第 2、3 条）：
    1. 学生/参考答案被修改后，已有的"已确认分类"应当退回待复核，
       但旧结论不能凭空消失，得有据可查；
    2. force=true 重跑分析会推翻旧结论，同样需要留痕。

本表只做**追加归档**，不参与任何统计口径；统计仍只看
error_items.review_flag（见 study_stats.is_confirmed_classification）。

用法：python src/backend/db/migrate_review_history.py
      python src/backend/db/migrate_review_history.py --check   # 只检查不建表
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import db as _dbconf                                     # noqa: E402
_dbconf.apply_cli_db()      # 让 --db 生效；不调这句会静默写到默认正式库
from db import get_conn, resolve_db_path                   # noqa: E402
DB_PATH = _dbconf.resolve_db_path()   # apply 之后再取，才是命令行给的那个          # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS error_review_history (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  error_item_id     INTEGER NOT NULL REFERENCES error_items(id) ON DELETE CASCADE,
  event             TEXT    NOT NULL,   -- answer_changed / reanalyzed / review_changed
  review_flag       TEXT,               -- 归档时刻的状态
  error_type        TEXT,               -- 归档时刻的错因 code
  error_confidence  REAL,              -- 归档时刻的置信度
  analysis          TEXT,               -- 归档时刻的解析正文
  wrong_answer_text TEXT,               -- 归档时刻的学生作答
  answer_text      TEXT,               -- 归档时刻的参考答案
  user_notes       TEXT,
  reason           TEXT,               -- 为什么被归档（写给人看）
  created_at       TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
"""
IDX = "CREATE INDEX IF NOT EXISTS idx_review_hist_item" \
      " ON error_review_history(error_item_id, created_at)"


def main():
    if "--check" in sys.argv:
        conn = get_conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
            " AND name='error_review_history'").fetchone()[0]
        conn.close()
        print(f"error_review_history {'已存在' if n else '不存在'}（库：{DB_PATH}）")
        return 0 if n else 1

    conn = get_conn()
    before = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
        " AND name='error_review_history'").fetchone()[0]
    conn.execute(DDL)
    conn.execute(IDX)
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(error_review_history)")]
    rows = conn.execute("SELECT COUNT(*) FROM error_review_history").fetchone()[0]
    conn.close()
    print(f"数据库：{DB_PATH}")
    print(f"error_review_history {'原已存在' if before else '已创建'}，字段 {len(cols)} 个")
    print(f"   {cols}")
    print(f"当前归档记录 {rows} 条")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
