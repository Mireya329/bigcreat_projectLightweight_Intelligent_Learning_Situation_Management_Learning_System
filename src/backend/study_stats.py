# -*- coding: utf-8 -*-
"""学情统计：一条 SQL 同时查单词线和错题线。

这个文件存在的意义，就是证明"单词和错题放在同一个库里"不是洁癖：
如果分两个库，"这个学生英语单词正确率多少 + 英语错题集中在哪" 这种问题
就得分别查两个库再在代码里拼，而现在只是几行 SQL。

对外提供 collect_stats()：返回结构化字典，供 study_stats 打印、
也供 ai_diagnose 喂给 AI —— 统计口径只有这一处定义。

用法：python src/backend/study_stats.py [用户名]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn                      # noqa: E402


def collect_stats(cur, uid: int) -> dict:
    """采集某个用户的跨线学情数据，返回结构化字典"""
    r = cur.execute(
        "SELECT COUNT(*), COUNT(DISTINCT word_id),"
        " SUM(wrong_count), AVG(total_time_ms)"
        " FROM word_records WHERE user_id=?", (uid,)).fetchone()
    total, distinct, wrongs, avg_ms = r
    perfect = cur.execute(
        "SELECT COUNT(*) FROM word_records WHERE user_id=? AND wrong_count=0",
        (uid,)).fetchone()[0]

    top_wrong = [{"word": row["word"], "wrong": row["s"], "count": row["c"]}
                 for row in cur.execute(
                     "SELECT w.word, SUM(r.wrong_count) s, COUNT(*) c"
                     " FROM word_records r JOIN words w ON r.word_id = w.id"
                     " WHERE r.user_id=? GROUP BY w.id"
                     " HAVING s > 0 ORDER BY s DESC LIMIT 5", (uid,))]

    by_subject = {row["name"] or "未分类": row["n"] for row in cur.execute(
        "SELECT s.name, COUNT(*) n FROM error_items e"
        " LEFT JOIN subjects s ON e.subject_id = s.id"
        " WHERE e.user_id=? GROUP BY s.name", (uid,))}

    by_mastery = {["未掌握", "复习中", "已掌握"][row[0]]: row[1]
                  for row in cur.execute(
                      "SELECT mastery_level, COUNT(*) FROM error_items"
                      " WHERE user_id=? GROUP BY mastery_level", (uid,))}

    # 错误原因分布（供前端饼图）；未分类的题单列，不混进具体类别
    type_label = {"concept": "概念不清", "calculation": "计算失误",
                  "misread": "审题偏差", "method": "方法缺失"}
    by_error_type = {}
    for row in cur.execute(
            "SELECT error_type, COUNT(*) n FROM error_items"
            " WHERE user_id=? GROUP BY error_type", (uid,)):
        by_error_type[type_label.get(row["error_type"], "未分类")] = row["n"]

    due = cur.execute(
        "SELECT COUNT(*) FROM review_schedules r"
        " JOIN error_items e ON r.error_item_id = e.id"
        " WHERE e.user_id=? AND r.completed_at IS NULL"
        " AND r.scheduled_for <= datetime('now','localtime')", (uid,)).fetchone()[0]

    ai_done = cur.execute(
        "SELECT COUNT(*) FROM error_items WHERE user_id=? AND ai_model=?",
        (uid, "qwen2.5:0.5b")).fetchone()[0]
    gated = cur.execute(
        "SELECT COUNT(*) FROM error_items WHERE user_id=? AND ai_model=?",
        (uid, "quality_gate")).fetchone()[0]
    manual = cur.execute(
        "SELECT COUNT(*) FROM error_items WHERE user_id=? AND ai_model=?",
        (uid, "manual")).fetchone()[0]

    return {
        "word": {
            "practice_count": total,
            "distinct_words": distinct,
            "perfect_rate": round(perfect / total * 100, 1) if total else 0.0,
            "total_wrong": wrongs or 0,
            "avg_sec_per_word": round((avg_ms or 0) / 1000, 2),
            "top_wrong_words": top_wrong,
        },
        "error": {
            "by_subject": by_subject,
            "by_mastery": by_mastery,
            "by_error_type": by_error_type,
            "due_review": due,
            "ai_parsed": ai_done,
            "gated": gated,
            "manual": manual,
        },
    }


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "test"
    conn = get_conn()
    cur = conn.cursor()
    uid = cur.execute(
        "SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]

    s = collect_stats(cur, uid)
    w, e = s["word"], s["error"]

    print("=" * 58)
    print(f"学情报告 · 用户 {username}")
    print("=" * 58)
    print("\n【单词线】")
    print(f"  练习次数      {w['practice_count']}")
    print(f"  涉及单词      {w['distinct_words']}")
    print(f"  一次打对率    {w['perfect_rate']}%")
    print(f"  累计出错      {w['total_wrong']} 次")
    print(f"  平均用时      {w['avg_sec_per_word']} 秒/词")
    if w["top_wrong_words"]:
        print("\n  最易错的词 TOP5：")
        for x in w["top_wrong_words"]:
            print(f"    {x['word']:<14} 错 {x['wrong']} 次 / 练 {x['count']} 次")

    print("\n【错题线】")
    for k, v in e["by_subject"].items():
        print(f"  {k}: {v} 道")
    for k, v in e["by_mastery"].items():
        print(f"  {k}: {v} 道")
    print(f"  待复习: {e['due_review']} 道")
    print(f"  AI 已解析 {e['ai_parsed']} 道 / 门禁拦下 {e['gated']} 道"
          f" / 人工补录 {e['manual']} 道")

    print("\n【跨线汇总】")
    print(f"  单词一次打对率 {w['perfect_rate']}%　|　"
          f"错题 {sum(e['by_subject'].values())} 道　|　"
          f"待处理 {e['due_review'] + e['gated']} 项")
    conn.close()


if __name__ == "__main__":
    main()
