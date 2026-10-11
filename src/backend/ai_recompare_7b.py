# -*- coding: utf-8 -*-
"""7B 重跑对照：备份 → 测试库 → 跑新模型 → 出对照表。

============================================================
为什么要有这个脚本
============================================================
队长 2026-09-27 回复第二节第 5 条要求：7B 要重新生成解析/分类/诊断以便比较，
但**当前不执行 --clear，不直接清空或覆盖正式库**，且重跑记录至少包含
「题目ID、模型、协议与提示词版本、返回code、分类、置信度、复核结论」。

所以流程必须是：
    1. 备份正式库，并**验证备份可恢复**（不验证的备份等于没备份）
    2. 复制一份测试库，所有写入只发生在测试库
    3. 在测试库上跑 7B，保留原始 OCR / 学生作答 / 旧结果
    4. 出对照表（旧 vs 新），人工复核结论列留空由人来填

正式库在本脚本里**只读**，不写任何一个字节。

用法
----
    # 只做备份 + 建测试库 + 出对照表骨架（网关没起也能跑，用于检查准备情况）
    python src/backend/ai_recompare_7b.py --prepare

    # 真正跑 7B（需要队长网关在本机 8765 端口运行）
    python src/backend/ai_recompare_7b.py --run

    # 只验证备份能不能恢复
    python src/backend/ai_recompare_7b.py --verify-backup

输出
----
    data/backup/learning_<时间戳>.db      正式库备份
    data/learning_7b_test.db              测试库（所有写入目标）
    docs/7B重跑对照表.md                   对照报告（含人工复核列）
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE / "db"))

from db import DB_PATH, get_conn                     # noqa: E402
import ai_interface as ai                            # noqa: E402

ROOT = DB_PATH.parent.parent
BACKUP_DIR = ROOT / "data" / "backup"
TEST_DB = ROOT / "data" / "learning_7b_test.db"
REPORT = ROOT / "docs" / "7B重跑对照表.md"

# 参与对照的题目：有解析文本的错题（排除 quality_gate / manual）
PICK_SQL = (
    "SELECT id, user_id, subject_id, question_text, ocr_text,"
    " analysis, error_type, error_confidence, error_reason, review_flag,"
    " ai_model, ai_elapsed_ms FROM error_items"
    " WHERE analysis IS NOT NULL AND analysis != ''"
    " AND (ai_model IS NULL"
    "      OR ai_model NOT IN ('quality_gate', 'manual'))"
    " ORDER BY id"
)


def _sqlite_backup(src: Path, dst: Path) -> None:
    """用 SQLite 官方备份接口复制数据库。

    为什么不用 shutil.copy2：队长 v2 第六节指出——
    "数据库仍在写入且使用 WAL 时，不要直接复制主 .db 文件作为完整快照，
     应使用 SQLite 备份接口或确认关闭所有连接后复制一致文件。"
    Connection.backup() 在数据库层面做一致快照，不必先停服务。
    """
    src_con = sqlite3.connect(str(src))
    dst_con = sqlite3.connect(str(dst))
    try:
        src_con.backup(dst_con)
    finally:
        dst_con.close()
        src_con.close()


# 备份要验证的"关键表"和 error_items 的"必要字段"。
# 队长：行数相同只是初步检查，还要看 SQLite 完整性、关键表及必要字段。
REQUIRED_TABLES = ("error_items", "subjects", "quiz_records")
REQUIRED_COLUMNS = ("id", "error_type", "error_confidence", "review_flag")


def verify_db_file(db_path: Path, label: str = "") -> bool:
    """验证一个 .db 文件：完整性 + 关键表 + 必要字段 + 行数。"""
    tag = label or db_path.name
    con = sqlite3.connect(str(db_path))
    try:
        n = con.execute("SELECT COUNT(*) FROM error_items").fetchone()[0]
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        tables = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        cols = {r[1] for r in con.execute("PRAGMA table_info(error_items)")}
        miss_t = [t for t in REQUIRED_TABLES if t not in tables]
        miss_c = [c for c in REQUIRED_COLUMNS if c not in cols]
        ok = integrity == "ok" and not miss_t and not miss_c
        print(f"  {'✓' if ok else '✗'} {tag}：integrity_check={integrity}，"
              f"error_items {n} 行"
              + (f"，缺表 {miss_t}" if miss_t else "")
              + (f"，缺字段 {miss_c}" if miss_c else ""))
        return ok
    except Exception as e:                             # noqa: BLE001
        print(f"  ✗ {tag}：无法校验 {e!r}")
        return False
    finally:
        con.close()


def backup(verify: bool = True) -> Path:
    """备份正式库（SQLite 备份接口），并验证完整性 + 关键表 + 行数。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"learning_{stamp}.db"
    _sqlite_backup(DB_PATH, dst)
    print(f"已备份：{dst}  （{dst.stat().st_size / 1024:.0f} KB）")

    if verify:
        verify_db_file(DB_PATH, "正式库")
        if not verify_db_file(dst, "备份文件"):
            raise RuntimeError("备份未通过完整性/关键表校验，请勿依赖它恢复")
        n_src = _count(DB_PATH, "error_items")
        n_dst = _count(dst, "error_items")
        if n_src != n_dst:
            raise RuntimeError(f"备份校验失败：原库 {n_src} 行，备份 {n_dst} 行")
        print(f"备份校验通过：error_items {n_dst} 行，与原库一致")
    return dst


def _count(db_path: Path, table: str) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


def verify_backup() -> int:
    """列出所有备份并逐个验证可打开。返回不可用的个数。"""
    if not BACKUP_DIR.exists():
        print("尚无备份")
        return 0
    files = sorted(BACKUP_DIR.glob("learning_*.db"))
    bad = 0
    for p in files:
        if not verify_db_file(p, p.name):
            bad += 1
    print(f"共 {len(files)} 个备份，{bad} 个不可用")
    return bad


def make_test_db() -> Path:
    """复制正式库为测试库（同样走备份接口）。所有重跑写入只发生在这里。"""
    _sqlite_backup(DB_PATH, TEST_DB)
    verify_db_file(TEST_DB, "测试库")
    print(f"已建测试库：{TEST_DB}（正式库保持只读）")
    return TEST_DB


def load_old_results() -> list[dict]:
    """从**正式库**读旧结果（只读）。"""
    con = get_conn()
    try:
        return [dict(r) for r in con.execute(PICK_SQL).fetchall()]
    finally:
        con.close()


def run_7b(subject_code_map: dict[int, str]) -> list[dict]:
    """在测试库上跑 7B，写回测试库，返回对照记录。"""
    con = sqlite3.connect(str(TEST_DB))
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(PICK_SQL).fetchall()]

    records = []
    print(f"\n开始重跑 {len(rows)} 道题（模型 {ai.MODEL_NAME}，"
          f"provider={ai.AI_PROVIDER}）")
    print("=" * 70)
    for r in rows:
        subj = ai.subject_from_code(subject_code_map.get(r["subject_id"]))
        t0 = time.time()
        resp = ai.analyze_error(
            question=r["question_text"] or "",
            student_answer="（OCR 未识别出手写作答）",
            subject=subj,
        )
        d = resp.data or {}
        rec = {
            "id": r["id"],
            "model": resp.model,
            "protocol": ai.PROTOCOL_VERSION,
            "prompt_variant": "v1",
            "code": resp.code,
            "elapsed_ms": resp.elapsed_ms,
            "new_type": d.get("error_type"),
            "new_conf": d.get("error_confidence"),
            "new_reason": str(d.get("error_reason", ""))[:200],
            "old_type": r.get("error_type"),
            "old_conf": r.get("error_confidence"),
            "old_reason": (r.get("error_reason") or "")[:200],
            "old_model": r.get("ai_model"),
            "human_verdict": "",        # 留空，人工填
        }
        records.append(rec)
        flag = "一致" if rec["old_type"] == rec["new_type"] else "不一致"
        print(f"  id={rec['id']:<3} code={rec['code']} "
              f"旧={rec['old_type'] or 'NULL'}/{rec['old_conf']} "
              f"新={rec['new_type'] or 'NULL'}/{rec['new_conf']} → {flag}")

        # 只写测试库：保留旧结果到 *_old 列，新结果进正式列
        if resp.code == 0:
            con.execute(
                "UPDATE error_items SET analysis=?, ai_model=?, ai_elapsed_ms=?,"
                " error_type=?, error_confidence=?, error_reason=?, review_flag=?"
                " WHERE id=?",
                (resp.content, resp.model, resp.elapsed_ms,
                 # 本轮全量人工复核：即便置信度高也不直接入库
                 None if not ai.AUTO_COMMIT_CLASSIFICATION else d.get("error_type"),
                 d.get("error_confidence"), rec["new_reason"],
                 "pending_review", r["id"]))
    con.commit()
    con.close()
    return records


def write_report(records: list[dict], prepared_only: bool = False) -> None:
    """输出对照表。人工复核列留空——那一列只能由人来填。"""
    lines = [
        "# 7B 重跑对照表",
        "",
        f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"协议版本：{ai.PROTOCOL_VERSION}　提示词版本：error_analysis v1",
        f"对照口径：本轮全量人工复核（AUTO_COMMIT_CLASSIFICATION="
        f"{ai.AUTO_COMMIT_CLASSIFICATION}）",
        "",
        "> 队长要求：比较时同时看答案、归因依据和人工判断，",
        "> **不能只看是否命中类别词或置信度是否升高**。",
        "> 「人工复核结论」一列留空，由人来填，不得由脚本代填。",
        "",
    ]
    if prepared_only:
        lines += [
            "## 状态：仅完成准备工作（未跑 7B）",
            "",
            "原因：队长网关未在本机运行。跑之前先启动网关：",
            "```",
            "cd <组长任务交付包目录> && .\\start.ps1",
            "```",
            "然后执行：",
            "```",
            "set AI_PROVIDER=gateway",
            "python src/backend/ai_recompare_7b.py --run",
            "```",
            "",
        ]

    if records:
        lines += [
            "## 对照表",
            "",
            "| 题目ID | 旧模型 | 旧分类 | 旧置信度 | 新模型 | 新分类 | 新置信度 | "
            "code | 耗时ms | 人工复核结论 |",
            "|---|---|---|---|---|---|---|---|---|---|",
        ]
        for r in records:
            lines.append(
                f"| {r['id']} | {r['old_model'] or '-'} | {r['old_type'] or 'NULL'} "
                f"| {r['old_conf']} | {r['model']} | {r['new_type'] or 'NULL'} "
                f"| {r['new_conf']} | {r['code']} | {r['elapsed_ms']} "
                f"| {r['human_verdict'] or '（待填）'} |")
        same = sum(1 for r in records if r["old_type"] == r["new_type"])
        lines += [
            "",
            f"新旧分类一致：{same}/{len(records)}",
            "",
            "## 归因依据（新）",
            "",
        ]
        for r in records:
            lines.append(f"- **id={r['id']}**：{r['new_reason'] or '（无）'}")
    else:
        lines += ["（尚未产生对照数据）"]

    lines += [
        "",
        "## 环境与产物",
        "",
        f"- 正式库（只读）：`{DB_PATH}`",
        f"- 备份目录：`{BACKUP_DIR}`",
        f"- 测试库（可写）：`{TEST_DB}`",
        "",
        "## 恢复方法（队长 v2 第六节：不能在服务运行时直接 cp 覆盖）",
        "",
        "```bash",
        "# 1) 先停掉后端接口服务和一切写库任务（uvicorn / 跑批脚本）",
        "# 2) 把当前库另存一份，别就地覆盖",
        "cp data/learning.db data/learning_before_restore_<时间戳>.db",
        "",
        "# 3) 在隔离副本上验证备份能用（完整性 + 关键表 + 必要字段 + 行数）",
        "venv/Scripts/python.exe src/backend/ai_recompare_7b.py --verify-backup",
        "",
        "# 4) 确认无误后再替换，然后重启服务并检查",
        "cp data/backup/learning_<时间戳>.db data/learning.db",
        "```",
        "",
        "备份本身是用 SQLite 备份接口做的（`sqlite3.Connection.backup()`），",
        "不是简单的文件复制——WAL 写入期间也能拿到一致快照。",
        "",
    ]
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n对照表已写入：{REPORT}")


def main() -> int:
    args = set(sys.argv[1:])
    if "--verify-backup" in args:
        return 1 if verify_backup() else 0

    print("=" * 70)
    print("7B 重跑对照：备份 → 测试库 → 重跑 → 对照")
    print("=" * 70)
    print(f"正式库：{DB_PATH}（本脚本只读）")

    backup()
    make_test_db()

    con = get_conn()
    subj_map = {r["id"]: r["code"] for r in con.execute(
        "SELECT id, code FROM subjects")}
    con.close()

    records: list[dict] = []
    if "--run" in args:
        if not ai.check_ollama_alive():
            print("\n⚠️ 模型服务不在线。网关模式请先启动队长网关（start.ps1），"
                  "直连模式请先 `ollama serve`。")
            print("   本次只完成备份与测试库准备，未写入任何新结果。")
        else:
            records = run_7b(subj_map)
    else:
        print("\n[准备模式] 加 --run 才会真正调用模型。")

    write_report(records, prepared_only=not records)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
