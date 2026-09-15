# -*- coding: utf-8 -*-
"""学情统计：一条 SQL 同时查单词线和错题线。

这个文件存在的意义，就是证明"单词和错题放在同一个库里"不是洁癖：
如果分两个库，"这个学生英语单词正确率多少 + 英语错题集中在哪" 这种问题
就得分别查两个库再在代码里拼，而现在只是几行 SQL。

用法：python src/backend/study_stats.py [用户名]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn                      # noqa: E402


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "test"
    conn = get_conn()
    cur = conn.cursor()
    uid = cur.execute(
        "SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]

    print("=" * 58)
    print(f"学情报告 · 用户 {username}")
    print("=" * 58)

    # ---------- 单词线 ----------
    r = cur.execute(
        "SELECT COUNT(*), COUNT(DISTINCT word_id),"
        " SUM(wrong_count), AVG(total_time_ms)"
        " FROM word_records WHERE user_id=?", (uid,)).fetchone()
    total, distinct, wrongs, avg_ms = r
    perfect = cur.execute(
        "SELECT COUNT(*) FROM word_records WHERE user_id=? AND wrong_count=0",
        (uid,)).fetchone()[0]
    rate = perfect / total * 100 if total else 0

    print("\n【单词线】")
    print(f"  练习次数      {total}")
    print(f"  涉及单词      {distinct}")
    print(f"  一次打对率    {rate:.1f}%（{perfect}/{total}）")
    print(f"  累计出错      {wrongs or 0} 次")
    print(f"  平均用时      {(avg_ms or 0)/1000:.1f} 秒/词")

    print("\n  最易错的词 TOP5：")
    for row in cur.execute(
            "SELECT w.word, SUM(r.wrong_count) s, COUNT(*) c"
            " FROM word_records r JOIN words w ON r.word_id = w.id"
            " WHERE r.user_id=? GROUP BY w.id"
            " HAVING s > 0 ORDER BY s DESC LIMIT 5", (uid,)):
        print(f"    {row['word']:<14} 错 {row['s']} 次 / 练 {row['c']} 次")

    # ---------- 错题线 ----------
    print("\n【错题线】")
    for row in cur.execute(
            "SELECT s.name, COUNT(*) n FROM error_items e"
            " LEFT JOIN subjects s ON e.subject_id = s.id"
            " WHERE e.user_id=? GROUP BY s.name", (uid,)):
        print(f"  {row['name'] or '(未分类)'}: {row['n']} 道")

    for row in cur.execute(
            "SELECT mastery_level, COUNT(*) FROM error_items"
            " WHERE user_id=? GROUP BY mastery_level ORDER BY mastery_level",
            (uid,)):
        label = {0: "未掌握", 1: "复习中", 2: "已掌握"}[row[0]]
        print(f"  {label}: {row[1]} 道")

    due = cur.execute(
        "SELECT COUNT(*) FROM review_schedules r"
        " JOIN error_items e ON r.error_item_id = e.id"
        " WHERE e.user_id=? AND r.completed_at IS NULL"
        " AND r.scheduled_for <= datetime('now','localtime')", (uid,)).fetchone()[0]
    print(f"  待复习: {due} 道")

    ai = cur.execute(
        "SELECT COUNT(*) FROM error_items"
        " WHERE user_id=? AND ai_model=?", (uid, "qwen2.5:0.5b")).fetchone()[0]
    gate = cur.execute(
        "SELECT COUNT(*) FROM error_items"
        " WHERE user_id=? AND ai_model='quality_gate'", (uid,)).fetchone()[0]
    print(f"  AI 已解析: {ai} 道 / 门禁拦下待人工: {gate} 道")

    # ---------- 跨线汇总 ----------
    print("\n【跨线汇总】(以下数据需单词线和错题线 JOIN/UNION 才能得到)")
    eng_err = cur.execute(
        "SELECT COUNT(*) FROM error_items e JOIN subjects s ON e.subject_id=s.id"
        " WHERE e.user_id=? AND s.name='英语'", (uid,)).fetchone()[0]
    print(f"  单词一次打对率 {rate:.1f}%　|　数学错题 "
          f"{cur.execute('SELECT COUNT(*) FROM error_items e JOIN subjects s ON e.subject_id=s.id WHERE e.user_id=? AND s.name=?', (uid, '数学')).fetchone()[0]} 道"
          f"　|　英语错题 {eng_err} 道")
    print(f"  待处理事项合计: {due + gate} 项（复习 + 人工补录）")

    conn.close()


if __name__ == "__main__":
    main()
