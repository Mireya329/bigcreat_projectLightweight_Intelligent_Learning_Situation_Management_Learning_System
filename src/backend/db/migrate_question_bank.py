# -*- coding: utf-8 -*-
"""迁移：新增 question_bank（公开真题题库表）。

背景（4 号《题库资料对接说明》2026-10-21 第二节）：
    error_items 是**错题本**（学生自己的错了的题），公开真题无处导入。
    四号已整理 554 个文件 / 236 套卷，需要一张正式题库表来接。

字段以四号建议为准，我侧补了 5 个（都注释了为什么）：
    no_in_paper / section / text_source / text_chars / derived
  · no_in_paper + section：切分脚本产出的定位信息，没有它题目无法回溯到卷内位置
  · text_source + text_chars：区分"文字版直接提取"与"扫描件待 OCR"，
    实测样本里 9 个解析 PDF 全部提取 0 字符，必须能一眼看出哪些是空壳
  · derived：切分脚本按选项二次切出来的题可靠性低，人工校对时优先看它们

增量约束 UNIQUE(subject_code, source_file, no_in_paper)：
    重跑导入不会重复插题（ocr_to_db 的教训——别一跑就清空）。

用法：python src/backend/db/migrate_question_bank.py
      python src/backend/db/migrate_question_bank.py --check   # 只检查不建表
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_conn, DB_PATH          # noqa: E402

DDL = """
CREATE TABLE IF NOT EXISTS question_bank (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_code  TEXT    NOT NULL,   -- POSTGRAD_POLITICS/POSTGRAD_MATH/POSTGRAD_ENGLISH/CET4/CET6
  year          INTEGER,            -- 考研年份（2010-2026）；四六级为空
  session       TEXT,               -- 四六级场次（2024-06）；考研为空
  paper_set     INTEGER,            -- 第 N 套（四六级）；考研为 1
  question_type TEXT,               -- choice/cloze/reading/translation/writing/compute/essay
  no_in_paper   TEXT,               -- 卷内题号（切分产出，可能是 "3" 或 "3+1"）
  section       TEXT,               -- 大题名（Part I / 一、单项选择题）
  question_text TEXT    NOT NULL,
  options       TEXT,               -- JSON 数组，如 ["A) ...", "B) ..."]
  answer_text   TEXT,               -- 参考答案（解析 PDF 为扫描件时为空）
  analysis      TEXT,               -- 解析正文（同上）
  source_file   TEXT    NOT NULL,   -- 真题 PDF 相对路径（相对题库根目录）
  analysis_file TEXT,               -- 解析 PDF 相对路径
  audio_file    TEXT,               -- 听力 MP3 相对路径（可能有多个套共用一份）
  text_source   TEXT    NOT NULL,   -- pdf_text / ocr / empty —— 题干文本从哪来
  text_chars    INTEGER NOT NULL DEFAULT 0,  -- 提取字符数；0=扫描件待 OCR
  derived       INTEGER NOT NULL DEFAULT 0,  -- 1=按选项二次切出，可靠性低
  status        TEXT    NOT NULL DEFAULT 'draft',  -- draft/ok/rejected
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE (subject_code, source_file, no_in_paper)
);
CREATE INDEX IF NOT EXISTS idx_qbank_subject
  ON question_bank(subject_code, year, session, paper_set);
CREATE INDEX IF NOT EXISTS idx_qbank_status
  ON question_bank(status, text_source);
"""

FIELDS = ["id", "subject_code", "year", "session", "paper_set", "question_type",
          "no_in_paper", "section", "question_text", "options", "answer_text",
          "analysis", "source_file", "analysis_file", "audio_file",
          "text_source", "text_chars", "derived", "status", "created_at"]


def table_exists(cur) -> bool:
    return bool(cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='question_bank'"
    ).fetchone())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不建表")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()
    exists = table_exists(cur)
    print(f"数据库：{DB_PATH}")
    print(f"question_bank 表：{'已存在' if exists else '不存在'}")

    if args.check:
        if exists:
            n = cur.execute("SELECT COUNT(*) FROM question_bank").fetchone()[0]
            cols = [r[1] for r in cur.execute("PRAGMA table_info(question_bank)")]
            missing = [f for f in FIELDS if f not in cols]
            print(f"  现有 {n} 行；字段缺失：{missing or '无'}")
        conn.close()
        return

    cur.executescript(DDL)
    conn.commit()
    cols = [r[1] for r in cur.execute("PRAGMA table_info(question_bank)")]
    missing = [f for f in FIELDS if f not in cols]
    print(f"建表完成。字段数 {len(cols)}；缺失：{missing or '无'}")
    print("integrity:", cur.execute("PRAGMA integrity_check").fetchone()[0])
    conn.close()


if __name__ == "__main__":
    main()
