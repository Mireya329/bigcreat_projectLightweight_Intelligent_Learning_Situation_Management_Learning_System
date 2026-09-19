# -*- coding: utf-8 -*-
"""迁移：给 error_items 增加 question_type（题型）字段。

背景：3 号《前端图表数据需求清单》里「错题分布柱状图」要求按
**科目 × 题型**（choice 选择题 / fill 填空题 / essay 解答题 / writing 作文）统计。
库里原本没有题型字段，只有 source 里带着"一、选择题 / 二、填空题 / 三、计算题"。

取值约定（与 3 号清单一致）：
    choice   选择题
    fill     填空题
    essay    解答题（含计算题、证明题等大题）
    writing  作文 / 写作
    NULL     识别不出（不瞎填）

用法：python src/backend/db/migrate_question_type.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import get_conn, DB_PATH          # noqa: E402

LABEL = {"choice": "选择题", "fill": "填空题",
         "essay": "解答题", "writing": "作文"}


def infer(source: str, question: str):
    """从来源和题干里推断题型；识别不出返回 None（不瞎填）"""
    s = f"{source or ''} {question or ''}"
    if "选择题" in s or "单选" in s or "多选" in s:
        return "choice"
    if "填空题" in s:
        return "fill"
    if "作文" in s or "写作" in s:
        return "writing"
    if "计算题" in s or "解答题" in s or "证明题" in s:
        return "essay"
    return None


def main():
    conn = get_conn()
    cur = conn.cursor()

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(error_items)")]
    if "question_type" not in cols:
        cur.execute("ALTER TABLE error_items ADD COLUMN question_type TEXT")
        conn.commit()
        print("[迁移] error_items 已增加 question_type 字段")

    rows = cur.execute("SELECT id, source, question_text FROM error_items").fetchall()
    n = 0
    for r in rows:
        t = infer(r["source"], r["question_text"])
        if t:
            cur.execute("UPDATE error_items SET question_type=? WHERE id=?",
                        (t, r["id"]))
            n += 1
    conn.commit()
    print(f"已为 {n}/{len(rows)} 道题标注题型")

    print("\n题型分布（按科目）：")
    for r in cur.execute(
            "SELECT s.code, e.question_type, COUNT(*) n"
            " FROM error_items e LEFT JOIN subjects s ON e.subject_id=s.id"
            " GROUP BY s.code, e.question_type ORDER BY s.code"):
        print(f"  {str(r['code']):<18} {LABEL.get(r['question_type'], '未识别'):<6} {r['n']} 道")
    print("\n数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
