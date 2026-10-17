# -*- coding: utf-8 -*-
"""错题入库：把切分结果写入 error_items，并打标签、排复习计划。

⚠️ 2026-10-17 安全化改造（队长第三轮回复第五节第 4 条点名）：
    旧版第 40 行是 `DELETE FROM error_items WHERE user_id=?`，
    每次导入都清空该用户全部历史错题，外键级联还会带走标签关联和复习计划。
    拿它来"补作答"会把已有数据删光，所以默认改为**增量导入**：
      · 已存在的题（同 source）只更新题干与质量分，不动作答/错因/复核状态
      · 新题才 INSERT + 打标签 + 排复习
    真要全量重建，必须显式加 --replace，且执行前自动备份数据库。

用法：
    python src/backend/ocr_to_db.py                # 增量导入（默认，不删任何东西）
    python src/backend/ocr_to_db.py --dry-run      # 只报告会新增/更新几条，不写库
    python src/backend/ocr_to_db.py --replace      # 全量重建（先自动备份，再清空重灌）
"""
import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                 # 为了 import ocr_split_v2
sys.path.insert(0, str(HERE / "db"))          # 为了 import db

from ocr_split_v2 import split_questions, SRC     # noqa: E402
from db import get_conn, DB_PATH                  # noqa: E402
from ocr_quality import score_text                # noqa: E402

USERNAME = "test"
SUBJECT = "考研数学"          # 2026-10-17：库里学科名是"考研数学"，旧值"数学"查不到
TAG = "偏导数与高阶偏导数"
SOURCE = "习题二 偏导数与高阶偏导数（真实试卷 OCR）"


def backup_db(reason: str) -> Path:
    """用 sqlite3 的 backup 接口做一致性备份（WAL 期间 shutil.copy 会不一致）。"""
    out_dir = DB_PATH.parent / "backups"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = out_dir / f"learning_{reason}_{stamp}.db"
    src = sqlite3.connect(str(DB_PATH))
    dst_conn = sqlite3.connect(str(dst))
    src.backup(dst_conn)
    dst_conn.close()
    src.close()
    return dst


def main():
    ap = argparse.ArgumentParser(description="OCR 切分结果入库（默认增量，不删历史）")
    ap.add_argument("--replace", action="store_true",
                    help="全量重建：清空该用户错题后重灌（会自动备份，慎用）")
    ap.add_argument("--dry-run", action="store_true",
                    help="只统计会新增/更新几条，不写库")
    args = ap.parse_args()

    conn = get_conn()
    cur = conn.cursor()

    # 0. 定位用户和学科（动态查 id，别写死）
    row = cur.execute(
        "SELECT id FROM users WHERE username=?", (USERNAME,)).fetchone()
    if row is None:
        sys.exit(f"找不到用户 {USERNAME!r}，先跑 db/seed.py")
    user_id = row[0]
    row = cur.execute(
        "SELECT id FROM subjects WHERE user_id=? AND name=?",
        (user_id, SUBJECT)).fetchone()
    if row is None:
        names = [r[0] for r in cur.execute(
            "SELECT name FROM subjects WHERE user_id=?", (user_id,))]
        sys.exit(f"找不到学科 {SUBJECT!r}；该用户现有学科：{names}")
    subject_id = row[0]
    tag_id = cur.execute(
        "SELECT id FROM knowledge_tags WHERE user_id=? AND name=?",
        (user_id, TAG)).fetchone()[0] if cur.execute(
        "SELECT COUNT(*) FROM knowledge_tags WHERE user_id=? AND name=?",
        (user_id, TAG)).fetchone()[0] else None

    if args.replace:
        # 全量重建：先备份，再删。备份失败就不删，避免"删了没备份"。
        bak = backup_db("before_replace")
        n_before = cur.execute(
            "SELECT COUNT(*) FROM error_items WHERE user_id=?", (user_id,)).fetchone()[0]
        cur.execute("DELETE FROM error_items WHERE user_id=?", (user_id,))
        print(f"--replace：已备份到 {bak.name}，删除该用户旧错题 {n_before} 条")
    else:
        n_before = cur.execute(
            "SELECT COUNT(*) FROM error_items WHERE user_id=?", (user_id,)).fetchone()[0]
        print(f"增量模式：该用户现有错题 {n_before} 条，不会删除任何历史数据")

    # 1. 切分
    qs = split_questions(SRC.read_text(encoding="utf-8"))
    print(f"切出 {len(qs)} 道，开始入库...\n")

    # 已有题目的 source -> id 映射，用于增量判断
    existing = {r["source"]: r["id"] for r in cur.execute(
        "SELECT id, source FROM error_items WHERE user_id=?", (user_id,))}

    added, updated, skipped = 0, 0, 0
    for q in qs:
        body = "\n".join(q["lines"])
        quality, _ = score_text(body)              # 入库时就算好质量分
        source = f"{SOURCE}｜{q['section']} 第{q['no']}题（行{q['start_line']}-{q['end_line']}）"

        if source in existing:
            # 增量：只更新题干和质量分。
            # 作答（wrong_answer_text / answer_text）、错因（error_type）、
            # 复核状态（review_flag）一律不动——那是人工录入和 AI 归因的成果，
            # 重跑 OCR 不能把它们冲掉。
            if not args.dry_run:
                cur.execute(
                    "UPDATE error_items SET question_text=?, ocr_text=?, ocr_quality=?,"
                    " updated_at=datetime('now','localtime') WHERE id=?",
                    (body, body, quality, existing[source]))
            updated += 1
            continue

        if args.dry_run:
            added += 1
            continue

        cur.execute(
            "INSERT INTO error_items"
            " (user_id, subject_id, question_text, ocr_text, source,"
            "  mastery_level, ocr_quality)"
            " VALUES (?,?,?,?,?,?,?)",
            (user_id, subject_id, body, body, source, 0, quality),
        )
        eid = cur.lastrowid
        added += 1

        if tag_id:
            cur.execute("INSERT INTO error_item_tags VALUES (?,?)", (eid, tag_id))

        # 艾宾浩斯：第一次复习排 3 天后
        cur.execute(
            "INSERT INTO review_schedules (error_item_id, scheduled_for)"
            " VALUES (?, datetime('now','localtime','+3 day'))", (eid,))

    if args.dry_run:
        conn.close()
        print(f"[dry-run] 将新增 {added} 条、更新 {updated} 条、跳过 {skipped} 条，未写库")
        return

    conn.commit()

    # 2. 验收
    print("入库结果：")
    rows = cur.execute(
        "SELECT e.id, e.source, length(e.question_text), s.name"
        " FROM error_items e JOIN subjects s ON e.subject_id = s.id"
        " WHERE e.user_id=? ORDER BY e.id", (user_id,)).fetchall()
    for r in rows:
        src = r["source"].split("｜")[-1]
        print(f"  id={r['id']:<3} {r['name']}  {r[2]:>4}字符  {src}")

    n_tag = cur.execute("SELECT COUNT(*) FROM error_item_tags").fetchone()[0]
    n_rev = cur.execute("SELECT COUNT(*) FROM review_schedules").fetchone()[0]
    print(f"\n本次新增 {added} 条、更新 {updated} 条；"
          f"库中共 {len(rows)} 条错题，标签关联 {n_tag} 条，复习计划 {n_rev} 条")
    print("数据库:", DB_PATH)

    conn.close()


if __name__ == "__main__":
    main()
