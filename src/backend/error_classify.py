# -*- coding: utf-8 -*-
"""错题自动分类：从 AI 解析文本里抽取错误原因，写入 error_type。

分类值（与组长 Prompt 的四类一致）：
    concept     概念不清
    calculation 计算失误
    misread     审题偏差
    method      方法缺失
    （抽不到时为 NULL，绝不瞎填）

**关键设计：识别"模板列举"陷阱**
实测发现 0.5b 经常把四类当模板一起列出来，例如：
    "错误原因归类：概念不清，计算失误，审题偏差，方法缺失"
这**不是分类，是背书**。如果不识别，id=17 这类题会被算成四类全占，
分类统计直接失真。所以：
    同一段里出现 ≥3 个精确类别词 → 判定为 template，分类置 NULL

同理，每个类别都记录**置信度**（error_type_conf）：
    high     只命中一个类别，可信
    low      命中多个取最多的，或靠宽松词推断
    template AI 在背模板，未真分类
    none     完全没有可用信息

用法：
    python src/backend/error_classify.py            # 演练，只看不写
    python src/backend/error_classify.py --apply    # 写入数据库
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn, DB_PATH          # noqa: E402

EXACT = {                                  # 精确类别词（四字）
    "concept": "概念不清",
    "calculation": "计算失误",
    "misread": "审题偏差",
    "method": "方法缺失",
}
LOOSE = {                                  # 宽松词（精确词没命中时的兜底）
    "concept": "概念",
    "calculation": "计算",
    "misread": "审题",
    "method": "方法",
}
LABEL = {
    "concept": "概念不清",
    "calculation": "计算失误",
    "misread": "审题偏差",
    "method": "方法缺失",
}


def classify(text: str):
    """返回 (error_type, confidence)。type 为 None 表示分不出。"""
    if not text or not text.strip():
        return None, "none"

    exact = {k: text.count(p) for k, p in EXACT.items()}
    n_types = sum(1 for v in exact.values() if v > 0)

    # 1. 模板列举陷阱：四类（或三类）一起出现 = 模型在背书，没真分类
    if n_types >= 3:
        return None, "template"

    # 2. 有精确类别词
    if n_types >= 1:
        ordered = sorted(exact.items(), key=lambda x: -x[1])
        top, second = ordered[0], ordered[1]
        return top[0], ("high" if second[1] == 0 else "low")

    # 3. 兜底：宽松词频
    loose = {k: text.count(p) for k, p in LOOSE.items()}
    ordered = sorted(loose.items(), key=lambda x: -x[1])
    if ordered[0][1] > 0:
        return ordered[0][0], "low"

    return None, "none"


def ensure_column(conn):
    """老库缺 error_type_conf 时自动加（幂等）"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(error_items)")]
    if "error_type_conf" not in cols:
        conn.execute("ALTER TABLE error_items ADD COLUMN error_type_conf TEXT")
        conn.commit()
        print("[迁移] error_items 已增加 error_type_conf 字段")


def main():
    apply_mode = "--apply" in sys.argv
    conn = get_conn()
    cur = conn.cursor()
    ensure_column(conn)

    # 只处理真正经过 AI 解析的题；
    # quality_gate（门禁拦下）的 analysis 是提示文案而非解析，manual 是人工补录，都不参与
    rows = cur.execute(
        "SELECT id, analysis, ai_model FROM error_items"
        " WHERE analysis IS NOT NULL AND analysis != ''"
        " AND (ai_model IS NULL"
        "      OR ai_model NOT IN ('quality_gate', 'manual'))"
        " ORDER BY id").fetchall()

    print(f"有解析文本的错题 {len(rows)} 道\n")
    dist = {}
    for r in rows:
        t, conf = classify(r["analysis"])
        dist.setdefault((t, conf), 0)
        dist[(t, conf)] += 1
        tag = LABEL.get(t, "未分类")
        print(f"  id={r['id']:<3} → {tag:<6} ({conf})")
        if apply_mode:
            cur.execute(
                "UPDATE error_items SET error_type=?, error_type_conf=?"
                " WHERE id=?", (t, conf, r["id"]))

    if apply_mode:
        conn.commit()
        print("\n已写入数据库")
    else:
        print("\n[演练模式] 加 --apply 写入")

    print("\n分布统计：")
    for (t, conf), n in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {LABEL.get(t, '未分类'):<6} {conf:<9} {n} 道")
    print("\n数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
