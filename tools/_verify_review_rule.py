# -*- coding: utf-8 -*-
"""用真实数据库验证"review_flag 优先于历史 error_type"这条口径。

队长 v2 第五节原话：
    "所有分类统计必须优先排除 review_flag=pending_review，
     即使 error_type 非空也不能计入。……请用实际数据库验证各查询，
     而非只看汇总图是否为空。"
"""
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "src", "backend")
DB = os.path.join(ROOT, "data", "learning.db")
sys.path.insert(0, BACKEND)

print("数据库：%s" % DB)
print("存在：%s" % os.path.exists(DB))
if not os.path.exists(DB):
    raise SystemExit(1)

con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
cur = con.cursor()

print("\n===== 1. error_items 全表分布（review_flag × error_type）=====")
rows = list(cur.execute(
    "SELECT review_flag, error_type, COUNT(*) n FROM error_items"
    " GROUP BY review_flag, error_type ORDER BY review_flag, error_type"))
for r in rows:
    print("  review_flag=%-16s error_type=%-24s n=%s"
          % (r["review_flag"], r["error_type"], r["n"]))

print("\n===== 2. 关键检查：有没有 error_type 非空但仍在待复核的行 =====")
danger = list(cur.execute(
    "SELECT id, error_type, error_confidence, review_flag FROM error_items"
    " WHERE review_flag='pending_review'"
    " AND error_type IS NOT NULL AND error_type != '' ORDER BY id"))
print("  共 %d 行（这类行最容易被误当成已确认分类）" % len(danger))
for r in danger:
    print("   id=%-4s error_type=%-22s conf=%-6s review_flag=%s"
          % (r["id"], r["error_type"], r["error_confidence"], r["review_flag"]))

print("\n===== 3. 各统计查询是否都排除了待复核 =====")
total = cur.execute("SELECT COUNT(*) FROM error_items").fetchone()[0]
pending = cur.execute(
    "SELECT COUNT(*) FROM error_items WHERE review_flag='pending_review'"
).fetchone()[0]
confirmed = cur.execute(
    "SELECT COUNT(*) FROM error_items WHERE error_type IS NOT NULL"
    " AND error_type != ''"
    " AND (review_flag IS NULL OR review_flag != 'pending_review')"
).fetchone()[0]
print("  总错题数            : %s" % total)
print("  待复核（含非空 type）: %s" % pending)
print("  已确认分类          : %s" % confirmed)
print("  恒等式 total == pending + (其余未分类) + confirmed ？ "
      "%s" % (total >= pending + confirmed))

print("\n===== 4. 调用 study_stats.summary 看接口实际返回 =====")
try:
    import study_stats
    con2 = sqlite3.connect(DB)
    con2.row_factory = sqlite3.Row
    s = study_stats.collect_stats(con2.cursor(), 1)
    con2.close()
    if isinstance(s, dict):
        print("  by_error_type        : %s" % s.get("by_error_type"))
        print("  confirmed_error_types: %s" % s.get("confirmed_error_types"))
        print("  pending_review       : %s" % s.get("pending_review"))
        if "error" in s:
            print("  error.by_error_type  : %s" % (s["error"] or {}).get("by_error_type"))
            print("  error.review_policy  : %s" % (s["error"] or {}).get("review_policy"))
    else:
        print("  返回类型：%s" % type(s))
except Exception as e:
    print("  调用失败：%r" % (e,))

print("\n===== 5. is_confirmed_classification 逐行判定 =====")
LABELS = {"concept_misunderstanding": "概念不清", "calculation_error": "计算失误",
          "misread_question": "审题偏差", "method_gap": "方法缺失"}


def is_confirmed(row):
    return row["review_flag"] in (None, "") and row["error_type"] in LABELS


allrows = list(cur.execute(
    "SELECT id, error_type, review_flag FROM error_items ORDER BY id"))
for r in allrows:
    print("  id=%-4s type=%-24s flag=%-16s → 已确认=%s"
          % (r["id"], r["error_type"], r["review_flag"], is_confirmed(r)))
con.close()
