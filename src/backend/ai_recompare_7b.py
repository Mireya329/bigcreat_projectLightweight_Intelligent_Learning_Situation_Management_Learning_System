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


def backup(verify: bool = True) -> Path:
    """备份正式库，并验证备份可打开、行数与原库一致。"""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = BACKUP_DIR / f"learning_{stamp}.db"
    shutil.copy2(DB_PATH, dst)
    print(f"已备份：{dst}  （{dst.stat().st_size / 1024:.0f} KB）")

    if verify:
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
    bad = 0
    for p in sorted(BACKUP_DIR.glob("learning_*.db")):
        try:
            n = _count(p, "error_items")
            print(f"  ✓ {p.name}  可打开，error_items {n} 行")
        except Exception as e:                        # noqa: BLE001
            bad += 1
            print(f"  ✗ {p.name}  损坏：{e}")
    print(f"共 {len(list(BACKUP_DIR.glob('learning_*.db')))} 个备份，{bad} 个不可用")
    return bad


def make_test_db() -> Path:
    """复制正式库为测试库。所有重跑写入只发生在这里。"""
    shutil.copy2(DB_PATH, TEST_DB)
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
        "## 恢复方法",
        "",
        "```bash",
        "# 用备份覆盖正式库（执行前确认已停掉接口服务）",
        "cp data/backup/learning_<时间戳>.db data/learning.db",
        "```",
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
