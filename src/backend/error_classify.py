# -*- coding: utf-8 -*-
"""错题自动分类：产出队长协议规定的 error_type 四 code + 置信度 + 判定依据。

============================================================
分类取值（队长 2026-09-21 协议冻结，四个 code，不可自行增删）
============================================================
    concept_misunderstanding  概念理解错误
    calculation_error         计算失误
    misread_question          审题偏差
    method_gap                方法缺失
    （分不出时为 NULL，绝不瞎填）

置信度 error_confidence（0~1）
-----------------------------
    0.85  结构化命中：解析文本里明确给出了 code（模型自报或规则精确命中）
    0.60  关键词强命中：只命中一个精确类别词
    0.40  弱命中：命中多个取最多的，或靠宽松词推断
    0.00  模板列举 / 完全没信息

    **error_confidence < 0.5 → 打"待人工复核"标签，error_type 置 NULL，不自动入库**

**关键设计：识别"模板列举"陷阱**
实测发现小模型经常把四类当模板一起列出来，例如：
    "错误原因归类：概念不清，计算失误，审题偏差，方法缺失"
这**不是分类，是背书**。如果不识别，同一道题会被算成四类全占，统计直接失真。
所以：同一段里出现 ≥3 个精确类别词 → confidence 置 0，分类置 NULL。

数据来源（两条路，本模块都支持）
--------------------------------
1. 新流程：ai_interface 返回的 data 里模型自报 error_type，渲染文本形如
   "【错误原因】计算失误（calculation_error，置信度 0.8）"——直接解析回来。
2. 旧数据：analysis 是模型自由文本，靠关键词统计兜底。

用法：
    python src/backend/error_classify.py              # 演练，只看不写
    python src/backend/error_classify.py --apply      # 写入数据库
    python src/backend/error_classify.py --migrate    # 只做旧值迁移
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn, DB_PATH          # noqa: E402

# ---- 四个 code（队长协议）----
ERROR_TYPE_CODES = {
    "concept_misunderstanding": "概念理解错误",
    "calculation_error": "计算失误",
    "misread_question": "审题偏差",
    "method_gap": "方法缺失",
}
LABEL = ERROR_TYPE_CODES                  # 兼容旧名

# ---- 旧取值 → 新 code（历史数据迁移用）----
LEGACY_MAP = {
    "concept": "concept_misunderstanding",
    "calculation": "calculation_error",
    "misread": "misread_question",
    "method": "method_gap",
    "conceptmisunderstanding": "concept_misunderstanding",
}

EXACT = {                                  # 精确类别词（四字）
    "concept_misunderstanding": "概念不清",
    "calculation_error": "计算失误",
    "misread_question": "审题偏差",
    "method_gap": "方法缺失",
}
LOOSE = {                                  # 宽松词（精确词没命中时的兜底）
    "concept_misunderstanding": "概念",
    "calculation_error": "计算",
    "misread_question": "审题",
    "method_gap": "方法",
}

CONF_STRUCTURED = 0.85                     # 文本里明确给了 code
CONF_STRONG = 0.60                         # 只命中一个精确类别词
CONF_WEAK = 0.40                           # 多命中取最多 / 宽松词推断
CONF_NONE = 0.00                           # 模板列举 / 无信息
THRESHOLD = 0.5                            # 低于此值不自动入库


# ------------------------------------------------------------
# 分类
# ------------------------------------------------------------
def classify(text: str):
    """返回 (error_type, confidence, reason)。

    error_type 为 None 表示分不出（调用方应置 NULL 并标待复核）。
    reason 是一句话判定依据（队长要求字段 error_reason）。
    """
    if not text or not text.strip():
        return None, CONF_NONE, "无解析文本，无法分类"

    # ---- 路径一：结构化解析（新流程渲染文本里就有 code 和置信度）----
    m = re.search(r"（\s*([a-z_]+)\s*，置信度\s*([0-9.]+)\s*）", text)
    if m and m.group(1) in ERROR_TYPE_CODES:
        code = m.group(1)
        try:
            conf = float(m.group(2))
        except ValueError:
            conf = CONF_STRUCTURED
        conf = min(max(conf, 0.0), 1.0)
        r = re.search(r"判定依据：(.+)", text)
        reason = r.group(1).strip()[:60] if r else "AI 自报分类"
        return code, conf, reason

    # JSON 风格的残留（模型偶尔把 code 裸写进文本）
    m2 = re.search(r'"error_type"\s*:\s*"([a-z_]+)"', text)
    if m2 and m2.group(1) in ERROR_TYPE_CODES:
        return m2.group(1), CONF_STRUCTURED, "解析文本中残留 JSON 字段"

    # ---- 路径二：关键词统计（旧自由文本）----
    exact = {k: text.count(v) for k, v in EXACT.items()}
    hit = {k: v for k, v in exact.items() if v > 0}

    # 模板列举陷阱：三类以上一起出现 = 模型在背书，没真分类
    if len(hit) >= 3:
        return None, CONF_NONE, "解析文本在列举全部类别，属模板复述而非真实分类"

    if len(hit) == 1:
        code = list(hit)[0]
        return code, CONF_STRONG, f"精确命中类别词「{EXACT[code]}」"

    if len(hit) == 2:
        ordered = sorted(hit.items(), key=lambda x: -x[1])
        code = ordered[0][0]
        return code, CONF_WEAK, (
            f"同时命中「{EXACT[code]}」与"
            f"「{EXACT[ordered[1][0]]}」，取出现次数较多者")

    loose = {k: text.count(v) for k, v in LOOSE.items()}
    loose_hit = {k: v for k, v in loose.items() if v > 0}
    if loose_hit:
        ordered = sorted(loose_hit.items(), key=lambda x: -x[1])
        code = ordered[0][0]
        return code, CONF_WEAK, f"仅命中宽松词「{LOOSE[code]}」，弱推断"

    return None, CONF_NONE, "未命中任何类别词"


def need_human_review(confidence: float) -> bool:
    """队长规则：置信度 < 0.5 → 待人工复核，不自动入库"""
    try:
        return float(confidence) < THRESHOLD
    except (TypeError, ValueError):
        return True


# ------------------------------------------------------------
# 数据库：列迁移
# ------------------------------------------------------------
NEW_COLUMNS = {
    "error_confidence": "REAL",
    "error_reason": "TEXT",
    "review_flag": "TEXT",               # 'pending_review' 或 NULL
}
# 历史版本用过的分级字段，保留不删
OLD_COLUMN = "error_type_conf"


def ensure_columns(conn):
    """老库缺列时自动加（幂等）"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(error_items)")]
    for col, typ in NEW_COLUMNS.items():
        if col not in cols:
            conn.execute(f"ALTER TABLE error_items ADD COLUMN {col} {typ}")
            print(f"[迁移] error_items 已增加 {col} {typ}")
    if OLD_COLUMN not in cols:
        conn.execute(f"ALTER TABLE error_items ADD COLUMN {OLD_COLUMN} TEXT")
        print(f"[迁移] error_items 已增加 {OLD_COLUMN} TEXT")
    conn.commit()


def migrate_legacy_values(conn) -> int:
    """把旧取值（concept/calculation/misread/method）迁到队长四 code。

    返回迁移行数。幂等：已经是新 code 的行不受影响。
    """
    cur = conn.cursor()
    n = 0
    for old, new in LEGACY_MAP.items():
        c = cur.execute(
            "UPDATE error_items SET error_type=? WHERE LOWER(error_type)=?",
            (new, old)).rowcount
        n += c
    # 大小写/空格脏值：Concept / Calculation / MethodMissing 之类
    dirty = cur.execute(
        "SELECT id, error_type FROM error_items"
        " WHERE error_type IS NOT NULL AND error_type != ''").fetchall()
    extra = 0
    for r in dirty:
        et = (r["error_type"] or "").strip()
        if et in ERROR_TYPE_CODES:
            continue
        key = re.sub(r"[^a-z]", "", et.lower())
        target = LEGACY_MAP.get(key) or (key if key in ERROR_TYPE_CODES else None)
        if target:
            cur.execute("UPDATE error_items SET error_type=? WHERE id=?",
                        (target, r["id"]))
            extra += 1
        elif key == "methodmissing":
            cur.execute("UPDATE error_items SET error_type='method_gap' WHERE id=?",
                        (r["id"],))
            extra += 1
    conn.commit()
    return n + extra


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------
def main():
    args = sys.argv[1:]
    apply_mode = "--apply" in args
    migrate_mode = "--migrate" in args

    conn = get_conn()
    cur = conn.cursor()
    ensure_columns(conn)

    if migrate_mode:
        n = migrate_legacy_values(conn)
        print(f"\n旧 error_type 迁移完成，共改写 {n} 行")
        conn.close()
        return

    # 只处理真正经过 AI 解析的题；
    # quality_gate（门禁拦下）的 analysis 是提示文案，manual 是人工补录，都不参与
    rows = cur.execute(
        "SELECT id, analysis, ai_model FROM error_items"
        " WHERE analysis IS NOT NULL AND analysis != ''"
        " AND (ai_model IS NULL"
        "      OR ai_model NOT IN ('quality_gate', 'manual'))"
        " ORDER BY id").fetchall()

    print(f"有解析文本的错题 {len(rows)} 道\n")
    dist: dict[tuple, int] = {}
    review_list = []

    for r in rows:
        code, conf, reason = classify(r["analysis"])
        review = need_human_review(conf)
        # 待复核的一律不写 error_type，只留置信度和依据
        final_type = None if review else code
        flag = "pending_review" if review else None
        dist.setdefault((final_type, round(conf, 2)), 0)
        dist[(final_type, round(conf, 2))] += 1

        tag = ERROR_TYPE_CODES.get(final_type, "未分类")
        mark = " ⚠️待复核" if review else ""
        print(f"  id={r['id']:<3} → {tag:<8} conf={conf:<5} {mark}")
        print(f"        依据：{reason}")
        if review:
            review_list.append(r["id"])

        if apply_mode:
            cur.execute(
                "UPDATE error_items"
                " SET error_type=?, error_confidence=?, error_reason=?,"
                "     review_flag=?, error_type_conf=?"
                " WHERE id=?",
                (final_type, conf, reason, flag,
                 "pending_review" if review else ("high" if conf >= 0.6 else "low"),
                 r["id"]))

    if apply_mode:
        conn.commit()
        print("\n已写入数据库")
        if review_list:
            print(f"待人工复核 {len(review_list)} 道（error_type 已置 NULL）："
                  f"{review_list[:20]}{' …' if len(review_list) > 20 else ''}")
    else:
        print("\n[演练模式] 加 --apply 写入；加 --migrate 只做旧值迁移")

    print("\n分布统计：")
    for (t, conf), n in sorted(dist.items(), key=lambda x: -x[1]):
        print(f"  {ERROR_TYPE_CODES.get(t, '未分类'):<8} conf={conf:<5} {n} 道")
    print(f"\n阈值 {THRESHOLD}：低于此值打「待人工复核」，error_type 不自动入库")
    print("数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
