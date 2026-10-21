# -*- coding: utf-8 -*-
"""迁移：question_bank 升级到 v2（对齐《题目主数据字段模板》19 列表头）。

为什么要有 v2（队长《给二号_Q01小批量试导交接要求》2026-10-21 第二节）：
    · 数学一二三、英语一二共用大类 code，必须有**卷型字段**才能区分，
      v1 把它们全塞进 POSTGRAD_MATH / POSTGRAD_ENGLISH，同 code 不同卷会混；
    · 每题要有**稳定的 source_question_id**，否则跨批次、跨机器无法定位同一题；
    · 页码、图片引用、音频引用、共享材料标识（听力/阅读共用材料）都要有字段，
      不能丢掉题目依赖的上下文；
    · v1 的字段名与四号 CSV 模板不一致，四号手工样例没法直接导入。

v1 → v2 字段映射（写在这里，避免两边各说一套）：
    question_text      -> stem
    options            -> options_json
    answer_text        -> answer
    analysis           -> explanation
    no_in_paper        -> question_no
    audio_file         -> audio_refs_json（字符串 -> JSON 数组）
    （新增）             paper_variant / source_question_id / source_pages_json
                        / print_page / image_refs_json / material_id
                        / knowledge_points_json / file_sha256 / updated_at
    （v1 独有保留）       section / text_source / text_chars / derived

运行保护（队长第四节）：
    · 单一配置：--db > LEARNING_DB_PATH > 默认正式库；写入前打印绝对路径；
    · 写库前自动备份（sqlite3 backup 接口，非文件拷贝）；
    · schema_migrations 记版本号，**重复执行直接跳过**，不会建两遍；
    · 旧表改名为 *_v1_archive，搬完数据且行数核对一致后才删，不静默丢数据。

用法：
    python src/backend/db/migrate_question_bank_v2.py --check
    python src/backend/db/migrate_question_bank_v2.py --db data/learning_test_qbank.db
"""
import argparse
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from db import get_conn, set_cli_db, announce_db      # noqa: E402
from db_backup import backup_database                 # noqa: E402

VERSION = "2026-10-21-question-bank-v2"

MIGRATIONS_DDL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
  version    TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  applied_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
  note       TEXT
);
"""

QBANK_V2_DDL = """
CREATE TABLE question_bank (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  -- ---- 四号 CSV 模板 19 列（顺序即模板顺序）----
  source_question_id   TEXT    NOT NULL UNIQUE,  -- 稳定主键，跨批次定位同一题
  subject_code         TEXT    NOT NULL,         -- 五类：POSTGRAD_POLITICS/POSTGRAD_MATH/
                                                 -- POSTGRAD_ENGLISH/CET4/CET6
  paper_variant        TEXT,                     -- 卷型：MATH_1/MATH_2/MATH_3/
                                                 -- ENGLISH_1/ENGLISH_2；政治与四六级为 NULL
  year                 INTEGER,                  -- 考研年份
  session              TEXT,                     -- 四六级场次，如 2024-06
  paper_set            INTEGER,                  -- 第 N 套（四六级）
  question_no          TEXT    NOT NULL,         -- 卷内题号，含子题，如 '36' / '36(1)'
  question_type        TEXT,                     -- single_choice/multi_choice/blank/
                                                 -- translation/writing/compute/answer
  knowledge_points_json TEXT,                    -- JSON 数组
  stem                 TEXT    NOT NULL,         -- 题干；听力题题干在音频里时可为空串
  options_json         TEXT,                     -- JSON 数组 ["A) ...", "B) ..."]
  answer               TEXT,                     -- 参考答案；主观题存文字答案
  explanation          TEXT,                     -- 解析
  source_file          TEXT    NOT NULL,         -- 真题 PDF 相对路径（相对题库根目录）
  source_pages_json    TEXT,                     -- PDF 页序数组，从 1 开始，如 [3,4]
  image_refs_json      TEXT,                     -- 图片相对路径数组
  audio_refs_json      TEXT,                     -- 音频相对路径数组
  material_id          TEXT,                     -- 共享材料标识（共用听力/共用阅读文章）
  status               TEXT    NOT NULL DEFAULT 'draft',  -- draft/pending_review/verified/rejected
  -- ---- 二号补充的溯源与审计字段 ----
  section              TEXT,                     -- 大题名（Part I / 一、单项选择题）
  analysis_file        TEXT,                     -- 解析 PDF 相对路径（答案来源，需人工核对）
  print_page           TEXT,                     -- 印刷页码（与 PDF 页序不同，另存）
  file_sha256          TEXT,                     -- 来源文件指纹，用于文件级去重
  text_source          TEXT    NOT NULL DEFAULT 'unknown',  -- pdf_text/ocr/manual/empty/unknown
  text_chars           INTEGER NOT NULL DEFAULT 0,   -- 提取字符数；0 = 没提出文字
  derived              INTEGER NOT NULL DEFAULT 0,   -- 1 = 按选项二次切出，可靠性低
  review_note          TEXT,                     -- 人工核对备注
  created_at           TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at           TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE (subject_code, source_file, question_no)
);
CREATE INDEX IF NOT EXISTS idx_qbank_lookup
  ON question_bank(subject_code, paper_variant, year, session, paper_set);
CREATE INDEX IF NOT EXISTS idx_qbank_status ON question_bank(status, text_source);
CREATE INDEX IF NOT EXISTS idx_qbank_material ON question_bank(material_id);
"""

CONFLICTS_DDL = """
CREATE TABLE IF NOT EXISTS question_bank_conflicts (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  run_id             TEXT,            -- 同一次导入的批次号
  source_question_id TEXT,
  source_file        TEXT,
  question_no        TEXT,
  field              TEXT,            -- 冲突字段
  reason             TEXT NOT NULL,   -- 为什么进待处理
  existing_value     TEXT,
  incoming_value     TEXT,
  state              TEXT NOT NULL DEFAULT 'open',   -- open/resolved
  created_at         TEXT NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_qbank_conflict_state
  ON question_bank_conflicts(state, created_at);
"""

# v1 -> v2 的字段搬运映射（v1 列名 -> v2 列名）
CARRY = [
    ("subject_code", "subject_code"),
    ("year", "year"),
    ("session", "session"),
    ("paper_set", "paper_set"),
    ("no_in_paper", "question_no"),
    ("section", "section"),
    ("question_type", "question_type"),
    ("question_text", "stem"),
    ("options", "options_json"),
    ("answer_text", "answer"),
    ("analysis", "explanation"),
    ("source_file", "source_file"),
    ("text_source", "text_source"),
    ("text_chars", "text_chars"),
    ("derived", "derived"),
    ("status", "status"),
    ("created_at", "created_at"),
]
OLD_MARKERS = ("question_text", "no_in_paper")   # 有这两列就是 v1 结构

# 后补的扩展字段：已经跑过本迁移的库再来一次时，靠这个补齐，不用重建表
EXTRA_COLUMNS = {
    "analysis_file": "TEXT",
}


def _ensure_extra_columns(cur) -> list:
    """给已存在的 v2 表补后加的列。返回补了哪些（没补返回空表）。"""
    cols = set(_cols(cur, "question_bank"))
    added = []
    for name, typ in EXTRA_COLUMNS.items():
        if name not in cols:
            cur.execute(f"ALTER TABLE question_bank ADD COLUMN {name} {typ}")
            added.append(name)
    return added


def _cols(cur, table):
    return [r[1] for r in cur.execute(f"PRAGMA table_info({table})")]


def _table_exists(cur, name):
    return bool(cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone())


def _mk_id(subject_code, variant, year, session, paper_set, question_no):
    """稳定 ID 规则（写死在这里，两边都按这条生成，才能对上）。"""
    variant = variant or "-"
    period = session or str(year or "-")
    pset = paper_set if paper_set is not None else "-"
    return "|".join([subject_code, variant, period, str(pset), str(question_no)])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="数据库路径（覆盖环境变量）")
    ap.add_argument("--check", action="store_true", help="只检查不写库")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份（不推荐）")
    args = ap.parse_args()

    set_cli_db(args.db)
    db_path = announce_db(role="题库迁移 v2")

    conn = get_conn()
    cur = conn.cursor()

    has_v2_marker = "source_question_id" in _cols(cur, "question_bank") \
        if _table_exists(cur, "question_bank") else False
    applied = None
    if _table_exists(cur, "schema_migrations"):
        applied = cur.execute(
            "SELECT version, applied_at FROM schema_migrations WHERE version=?",
            (VERSION,)).fetchone()

    if args.check:
        print(f"question_bank 是否 v2 结构：{'是' if has_v2_marker else '否'}")
        print(f"迁移 {VERSION} 是否已执行：{'是，' + applied['applied_at'] if applied else '否'}")
        if _table_exists(cur, "question_bank"):
            print("现有行数：", cur.execute(
                "SELECT COUNT(*) FROM question_bank").fetchone()[0])
        conn.close()
        return

    if applied and has_v2_marker:
        print(f"迁移 {VERSION} 已于 {applied['applied_at']} 执行过（重复执行安全）")
        added = _ensure_extra_columns(cur)
        # 上次跑完可能留下空的 v1 归档表（0 行时旧代码没删），这里收尾
        if _table_exists(cur, "question_bank_v1_archive"):
            n = cur.execute(
                "SELECT COUNT(*) FROM question_bank_v1_archive").fetchone()[0]
            if n == 0:
                cur.execute("DROP TABLE question_bank_v1_archive")
                print("清理遗留的空归档表 question_bank_v1_archive")
            else:
                print(f"⚠️ 归档表 question_bank_v1_archive 仍有 {n} 行，保留未删")
        conn.commit()
        print(f"补齐后加字段：{added or '无'}")
        print(f"v2 字段数：{len(_cols(cur, 'question_bank'))}")
        conn.close()
        return

    # ---- 1. 备份 ----
    if not args.no_backup and db_path.exists():
        backup_database("before_qbank_v2", db_path)
    elif not db_path.exists():
        print(f"库不存在，将新建：{db_path}")

    # ---- 2. 建版本表 ----
    cur.executescript(MIGRATIONS_DDL)

    # ---- 3. 旧表改名留档，再建 v2 ----
    old_rows = []
    old_count = 0
    if _table_exists(cur, "question_bank"):
        cols = _cols(cur, "question_bank")
        if all(m in cols for m in OLD_MARKERS):
            old_count = cur.execute(
                "SELECT COUNT(*) FROM question_bank").fetchone()[0]
            old_rows = [dict(r) for r in cur.execute("SELECT * FROM question_bank")]
            cur.execute("ALTER TABLE question_bank RENAME TO question_bank_v1_archive")
            print(f"旧 v1 表改名为 question_bank_v1_archive（{old_count} 行待搬运）")
        elif has_v2_marker:
            print("已是 v2 结构，不再重建表")

    if not has_v2_marker:
        cur.executescript(QBANK_V2_DDL)
    cur.executescript(CONFLICTS_DDL)

    # ---- 4. 搬数据 ----
    moved = 0
    if old_rows:
        import json
        for r in old_rows:
            audio = r.get("audio_file")
            rec = {}
            for old, new in CARRY:
                rec[new] = r.get(old)
            rec["source_question_id"] = _mk_id(
                r.get("subject_code"), None, r.get("year"),
                r.get("session"), r.get("paper_set"), r.get("no_in_paper"))
            rec["audio_refs_json"] = json.dumps([audio], ensure_ascii=False) \
                if audio else None
            rec["print_page"] = None
            rec["file_sha256"] = None
            # updated_at 是 NOT NULL：旧库没这个值就让 DEFAULT 生效，别塞 None
            if r.get("created_at"):
                rec["updated_at"] = r["created_at"]
            keys = ",".join(rec.keys())
            marks = ",".join("?" for _ in rec)
            try:
                cur.execute(
                    f"INSERT OR IGNORE INTO question_bank ({keys}) VALUES ({marks})",
                    list(rec.values()))
                moved += cur.rowcount
            except sqlite3.IntegrityError as e:
                print(f"  ! 跳过一行（{e}）：{rec.get('source_question_id')}")
        if moved != old_count:
            conn.rollback()
            raise SystemExit(
                f"搬运行数不一致（旧 {old_count} / 新 {moved}），已回滚，"
                f"旧表仍保留为 question_bank_v1_archive")

    cur.execute("INSERT OR REPLACE INTO schema_migrations (version, name, note)"
                " VALUES (?,?,?)",
                (VERSION, "question_bank v2",
                 f"由 v1 搬运 {moved} 行；字段对齐题目主数据字段模板"))
    conn.commit()

    # ---- 5. 校验后才删旧表 ----
    integrity = cur.execute("PRAGMA integrity_check").fetchone()[0]
    new_count = cur.execute("SELECT COUNT(*) FROM question_bank").fetchone()[0]
    print(f"integrity_check：{integrity}")
    print(f"v2 表现有行数：{new_count}（由 v1 搬运 {moved} 行）")
    if old_rows:
        if new_count == old_count and integrity == "ok":
            cur.execute("DROP TABLE question_bank_v1_archive")
            conn.commit()
            print(f"行数核对一致，已删除归档表 question_bank_v1_archive")
        else:
            print("行数不一致或校验未过，**保留** question_bank_v1_archive，未删除")

    cols = _cols(cur, "question_bank")
    print(f"v2 字段（{len(cols)}）：{cols}")
    conn.close()


if __name__ == "__main__":
    main()
