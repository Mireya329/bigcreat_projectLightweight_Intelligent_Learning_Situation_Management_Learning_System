# -*- coding: utf-8 -*-
"""题库 CSV 导入：按《题目主数据字段模板_待二号确认.csv》的 19 列表头入库。

给四号用：手工整理好的题目主数据存成 CSV，这个文件负责校验 + 入 question_bank。
与 PDF 自动导入（question_bank_import.py）共用同一张表和同一套保护规则：
单一配置打印路径、--dry-run、逐行错误报告、幂等、非草稿态不覆盖。

字段口径（**这些规则就是给四号的填写说明，改这里等于改口径**）：
    source_question_id  必填，稳定主键；建议 {科目}|{卷型}|{场次或年份}|{套数}|{题号}
    subject_code        必填，五选一
    paper_variant       卷型：MATH_1/MATH_2/MATH_3/ENGLISH_1/ENGLISH_2；政治与四六级留空
    question_no         必填，卷内题号，含子题（"36" / "36(1)"）
    stem                必填；听力题题干确在音频里时填空串并在 review_note 说明
    options_json        JSON 数组字符串，元素形如 "A) xxx"；无选项留空
    answer              选择题存字母串（单选 "A"，多选 "ABD"）；
                        主观题存文字答案。**不用精确字符串比较判主观题**
    question_type       single_choice/multi_choice/true_false/blank/
                        short_answer/translation/writing/compute
    status              draft/pending_review/verified/rejected，默认 draft
    source_pages_json   JSON 数组，PDF 页序从 1 开始，如 [3,4]
    image_refs_json / audio_refs_json   JSON 数组，元素为**相对题库根目录**的路径
    material_id         共享材料标识（共用听力 / 共用阅读文章），没有留空

用法：
    # 先出一份带表头和 2 条样例的模板给四号填
    python src/backend/question_bank_csv.py --sample docs/题目主数据_样例.csv
    # 只校验不写库
    python src/backend/question_bank_csv.py 四号数据.csv --dry-run --report docs/logs/x.json
    # 真跑（独立测试库）
    python src/backend/question_bank_csv.py 四号数据.csv --db data/learning_test_qbank_v3.db
"""
import argparse
import csv
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "db"))
from db import set_cli_db, announce_db, get_conn     # noqa: E402

# 与《题目主数据字段模板_待二号确认.csv》完全一致的 19 列
COLUMNS = ["source_question_id", "subject_code", "paper_variant", "year",
           "session", "paper_set", "question_no", "question_type",
           "knowledge_points_json", "stem", "options_json", "answer",
           "explanation", "source_file", "source_pages_json",
           "image_refs_json", "audio_refs_json", "material_id", "status"]

REQUIRED = ["source_question_id", "subject_code", "question_no", "stem",
            "source_file"]
SUBJECT_CODES = {"POSTGRAD_POLITICS", "POSTGRAD_MATH", "POSTGRAD_ENGLISH",
                 "CET4", "CET6"}
VARIANTS = {"MATH_1", "MATH_2", "MATH_3", "ENGLISH_1", "ENGLISH_2"}
Q_TYPES = {"single_choice", "multi_choice", "true_false", "blank",
           "short_answer", "translation", "writing", "compute"}
STATUSES = {"draft", "pending_review", "verified", "rejected"}
INT_FIELDS = ("year", "paper_set")
JSON_FIELDS = ("knowledge_points_json", "options_json", "source_pages_json",
               "image_refs_json", "audio_refs_json")
LOCKED_STATUS = ("pending_review", "verified")

SAMPLE_ROWS = [
    {"source_question_id": "CET4|-|2024-06|1|1",
     "subject_code": "CET4", "paper_variant": "", "year": "2024",
     "session": "2024-06", "paper_set": "1", "question_no": "1",
     "question_type": "single_choice", "knowledge_points_json": "",
     "stem": "（听力题：题干在音频里，此处填从音频转写的题干或留空）",
     "options_json": '["A) He is a teacher", "B) He is a driver", '
                     '"C) He is a doctor", "D) He is a cook"]',
     "answer": "A", "explanation": "",
     "source_file": "试导样本/英语四级/2024-06/四级_2024-06_第1套_真题.pdf",
     "source_pages_json": "[1]",
     "image_refs_json": "",
     "audio_refs_json": '["试导样本/英语四级/2024-06/四级_2024-06_第1套_听力.mp3"]',
     "material_id": "四级|2024-06|listening|四级_2024-06_第1套",
     "status": "draft"},
    {"source_question_id": "POSTGRAD_MATH|MATH_2|2022|1|1",
     "subject_code": "POSTGRAD_MATH", "paper_variant": "MATH_2", "year": "2022",
     "session": "", "paper_set": "1", "question_no": "1",
     "question_type": "single_choice", "knowledge_points_json": "",
     "stem": "（数学真题为扫描件，此处由人工转写）",
     "options_json": '["A) 0", "B) 1", "C) 2", "D) 不存在"]',
     "answer": "B", "explanation": "",
     "source_file": "考研数学二/2022/数学二_2022_真题.pdf",
     "source_pages_json": "[1]", "image_refs_json": "", "audio_refs_json": "",
     "material_id": "", "status": "draft"},
]


def check_row(row: dict, lineno: int):
    """校验一行，返回错误列表（每条含 行号/字段/原因）。"""
    errs = []
    for f in REQUIRED:
        if not (row.get(f) or "").strip():
            errs.append({"line": lineno, "field": f, "reason": "必填为空"})
    if row.get("subject_code") and row["subject_code"] not in SUBJECT_CODES:
        errs.append({"line": lineno, "field": "subject_code",
                     "reason": f"{row['subject_code']} 不在五类之内"
                               f"（{'/'.join(sorted(SUBJECT_CODES))}）"})
    if row.get("paper_variant") and row["paper_variant"] not in VARIANTS:
        errs.append({"line": lineno, "field": "paper_variant",
                     "reason": f"{row['paper_variant']} 不是合法卷型"})
    if row.get("question_type") and row["question_type"] not in Q_TYPES:
        errs.append({"line": lineno, "field": "question_type",
                     "reason": f"{row['question_type']} 不是合法题型"})
    if row.get("status") and row["status"] not in STATUSES:
        errs.append({"line": lineno, "field": "status",
                     "reason": f"{row['status']} 不是合法状态"})
    for f in INT_FIELDS:
        v = (row.get(f) or "").strip()
        if v:
            try:
                int(v)
            except ValueError:
                errs.append({"line": lineno, "field": f,
                             "reason": f"{v!r} 不是整数"})
    for f in JSON_FIELDS:
        v = (row.get(f) or "").strip()
        if v:
            try:
                json.loads(v)
            except json.JSONDecodeError as e:
                errs.append({"line": lineno, "field": f,
                             "reason": f"不是合法 JSON：{e}"})
    # 答案口径：选择题存字母串，主观题存文字。混用会让判分规则失效。
    qt = row.get("question_type")
    ans = (row.get("answer") or "").strip()
    if qt in ("single_choice", "multi_choice", "true_false") and ans:
        bad = [ch for ch in ans if ch not in "ABCD"]
        if bad:
            errs.append({"line": lineno, "field": "answer",
                         "reason": "选择题答案只认 A-D 字母串，出现 " + str(bad)})
        if qt == "single_choice" and len(ans) != 1:
            errs.append({"line": lineno, "field": "answer",
                         "reason": f"单选答案应为单个字母，实际 {ans!r}"})
        if qt == "multi_choice" and len(ans) < 2:
            errs.append({"line": lineno, "field": "answer",
                         "reason": "多选答案至少两个字母"})
    return errs


def write_sample(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        for r in SAMPLE_ROWS:
            w.writerow(r)
    print(f"样例模板已写出：{path.resolve()}")
    print("表头（19 列，与四号模板一致）：")
    print("  " + ",".join(COLUMNS))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv", nargs="?", help="待导入的 CSV")
    ap.add_argument("--db", default=None, help="数据库路径（覆盖环境变量）")
    ap.add_argument("--dry-run", action="store_true", help="只校验不写库")
    ap.add_argument("--commit-each", action="store_true", help="逐条提交")
    ap.add_argument("--report", default=None, help="导出 JSON 报告")
    ap.add_argument("--sample", default=None, help="只写出样例模板后退出")
    args = ap.parse_args()

    if args.sample:
        write_sample(Path(args.sample))
        return
    if not args.csv:
        ap.error("缺少 CSV 路径（或改用 --sample 生成模板）")

    csv_path = Path(args.csv).resolve()
    if not csv_path.is_file():
        sys.exit(f"CSV 不存在：{csv_path}")

    set_cli_db(args.db)
    db_path = announce_db(role="题库 CSV 导入")
    print(f"CSV 文件：{csv_path}")

    with csv_path.open(encoding="utf-8-sig", newline="") as f:
        rd = csv.DictReader(f)
        header = rd.fieldnames or []
        missing = [c for c in COLUMNS if c not in header]
        extra = [c for c in header if c not in COLUMNS]
        rows = list(rd)

    print(f"表头列数 {len(header)}；缺列：{missing or '无'}；多出列：{extra or '无'}")
    if missing:
        sys.exit(f"CSV 缺列 {missing}，先对齐模板（可用 --sample 生成一份对照）")

    conn = get_conn()
    cur = conn.cursor()
    if not cur.execute("SELECT name FROM sqlite_master WHERE type='table'"
                       " AND name='question_bank'").fetchone():
        sys.exit("当前库没有 question_bank 表，先跑 "
                 "python src/backend/db/migrate_question_bank_v2.py")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    stats = {"rows": len(rows), "inserted": 0, "updated": 0, "failed": 0,
             "skipped_conflict": 0}
    errors, conflicts = [], []

    for i, rd_row in enumerate(rows, start=2):        # 第 1 行是表头
        row = {k: (rd_row.get(k) or "").strip() for k in COLUMNS}
        errs = check_row(row, i)
        if errs:
            errors.extend(errs)
            stats["failed"] += 1
            continue
        rec = {
            "source_question_id": row["source_question_id"],
            "subject_code": row["subject_code"],
            "paper_variant": row["paper_variant"] or None,
            "year": int(row["year"]) if row["year"] else None,
            "session": row["session"] or None,
            "paper_set": int(row["paper_set"]) if row["paper_set"] else None,
            "question_no": row["question_no"],
            "question_type": row["question_type"] or None,
            "knowledge_points_json": row["knowledge_points_json"] or None,
            "stem": row["stem"],
            "options_json": row["options_json"] or None,
            "answer": row["answer"] or None,
            "explanation": row["explanation"] or None,
            "source_file": row["source_file"],
            "source_pages_json": row["source_pages_json"] or None,
            "image_refs_json": row["image_refs_json"] or None,
            "audio_refs_json": row["audio_refs_json"] or None,
            "material_id": row["material_id"] or None,
            "status": row["status"] or "draft",
            "text_source": "manual",      # 人工整理，与 pdf_text / ocr / empty 区分
            "text_chars": len(row["stem"]),
        }
        if args.dry_run:
            continue
        old = cur.execute("SELECT id, status, stem, answer FROM question_bank"
                          " WHERE source_question_id=?",
                          (rec["source_question_id"],)).fetchone()
        try:
            if old:
                if old["status"] in LOCKED_STATUS:
                    stats["skipped_conflict"] += 1
                    conflicts.append({
                        "source_question_id": rec["source_question_id"],
                        "source_file": rec["source_file"],
                        "question_no": rec["question_no"], "field": "整行",
                        "reason": f"库中 status={old['status']}，非草稿态，导入不覆盖",
                        "existing_value": (old["stem"] or "")[:80],
                        "incoming_value": rec["stem"][:80]})
                    continue
                sets = ",".join(f"{k}=:{k}" for k in rec)
                cur.execute(f"UPDATE question_bank SET {sets},"
                            " updated_at=datetime('now','localtime')"
                            " WHERE id=:id", {**rec, "id": old["id"]})
                stats["updated"] += 1
            else:
                keys = ",".join(rec.keys())
                marks = ",".join(":" + k for k in rec.keys())
                cur.execute(f"INSERT INTO question_bank ({keys}) VALUES ({marks})", rec)
                stats["inserted"] += 1
        except sqlite3.Error as e:
            stats["failed"] += 1
            errors.append({"line": i, "field": "DB",
                           "reason": f"{type(e).__name__} {e}"})
            continue
        if args.commit_each:
            conn.commit()

    if args.dry_run:
        conn.rollback()
        print("\n事务：--dry-run，未写入任何数据")
    else:
        conn.commit()
        print(f"\n事务：{'逐条提交' if args.commit_each else '整批提交成功'}")
        for c in conflicts:
            cur.execute(
                "INSERT INTO question_bank_conflicts (run_id, source_question_id,"
                " source_file, question_no, field, reason, existing_value,"
                " incoming_value) VALUES (?,?,?,?,?,?,?,?)",
                (run_id, c.get("source_question_id"), c.get("source_file"),
                 c.get("question_no"), c.get("field"), c.get("reason"),
                 c.get("existing_value"), c.get("incoming_value")))
        conn.commit()

    total = cur.execute("SELECT COUNT(*) FROM question_bank").fetchone()[0]
    conn.close()

    print("=" * 62)
    print(f"CSV 行数 {stats['rows']}，字段错误 {len(errors)} 条")
    if not args.dry_run:
        print(f"入库：新增 {stats['inserted']}，更新 {stats['updated']}，"
              f"非草稿态跳过 {stats['skipped_conflict']}，失败 {stats['failed']}")
        print(f"question_bank 现有总行数：{total}")
    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(json.dumps({"run_id": run_id, "database": str(db_path),
                                  "csv": str(csv_path), "stats": stats,
                                  "errors": errors, "conflicts": conflicts},
                                 ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已导出：{rp.resolve()}")
    for e in errors[:10]:
        print(f"  - 第 {e.get('line')} 行 [{e.get('field')}] {e.get('reason')}")


if __name__ == "__main__":
    main()
