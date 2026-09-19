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

    # ---- 以下为响应 3 号《前端图表数据需求清单》（2026-09-19）新增 ----

    # 科目 × 题型（柱状图）
    by_question_type: dict = {}
    for row in cur.execute(
            "SELECT s.code, e.question_type, COUNT(*) n FROM error_items e"
            " LEFT JOIN subjects s ON e.subject_id=s.id WHERE e.user_id=?"
            " GROUP BY s.code, e.question_type", (uid,)):
        code = row["code"] or "UNKNOWN"
        slot = by_question_type.setdefault(
            code, {"choice": 0, "fill": 0, "essay": 0, "writing": 0, "unknown": 0})
        slot[row["question_type"] or "unknown"] = row["n"]

    # 薄弱考点 TOP10（横向条形图）
    weak_points = [{"point": r["name"], "subject_code": r["code"], "count": r["n"]}
                   for r in cur.execute(
                       "SELECT t.name, s.code, COUNT(*) n FROM error_item_tags et"
                       " JOIN knowledge_tags t ON et.tag_id=t.id"
                       " JOIN error_items e ON et.error_item_id=e.id"
                       " LEFT JOIN subjects s ON e.subject_id=s.id"
                       " WHERE e.user_id=? GROUP BY t.id ORDER BY n DESC LIMIT 10",
                       (uid,))]

    # 雷达图：五科目 × 六维度（0-100）。算不出的维度返回 None，绝不用随机数填充
    radar: dict = {}
    codes = [r["code"] for r in cur.execute(
        "SELECT DISTINCT code FROM subjects WHERE user_id=? AND code IS NOT NULL",
        (uid,))]

    def mastered_of(code, qtype):
        row = cur.execute(
            "SELECT COUNT(*) total,"
            " SUM(CASE WHEN mastery_level >= 1 THEN 1 ELSE 0 END) ok"
            " FROM error_items e JOIN subjects s ON e.subject_id=s.id"
            " WHERE e.user_id=? AND s.code=? AND e.question_type=?",
            (uid, code, qtype)).fetchone()
        total = row["total"] or 0
        return round(row["ok"] / total * 100, 1) if total else None

    for code in codes:
        total = cur.execute(
            "SELECT COUNT(*) n FROM error_items e JOIN subjects s"
            " ON e.subject_id=s.id WHERE e.user_id=? AND s.code=?",
            (uid, code)).fetchone()["n"]
        calc_wrong = cur.execute(
            "SELECT COUNT(*) n FROM error_items e JOIN subjects s"
            " ON e.subject_id=s.id WHERE e.user_id=? AND s.code=?"
            " AND e.error_type='calculation'", (uid, code)).fetchone()["n"]
        radar[code] = {
            # 单词维度目前只有英语类科目有数据，其余给 None
            "vocabulary": (round(perfect / total * 100, 1)
                           if total and code in ("CET4", "CET6", "POSTGRAD_ENGLISH")
                           else None),
            "choice": mastered_of(code, "choice"),
            "essay": mastered_of(code, "essay"),
            "recite": None,      # 无背诵数据源
            "calculate": (round(100 - calc_wrong / total * 100, 1) if total else None),
            "writing": None,     # 无写作数据源
        }

    # 刷题聚合（趋势图 + 正确率）
    q = cur.execute(
        "SELECT COUNT(*) sessions, SUM(total_count) total,"
        " SUM(correct_count) correct FROM quiz_records WHERE user_id=?",
        (uid,)).fetchone()
    q_total, q_correct = q["total"] or 0, q["correct"] or 0

    # 学习计时聚合（时长图）
    s_today = cur.execute(
        "SELECT SUM(duration_sec) sec FROM study_sessions"
        " WHERE user_id=? AND studied_at=date('now','localtime')",
        (uid,)).fetchone()["sec"] or 0
    s_all = cur.execute(
        "SELECT SUM(duration_sec) sec FROM study_sessions WHERE user_id=?",
        (uid,)).fetchone()["sec"] or 0

    # 页面顶部汇总（算不出的仍给 None，不编造）
    summary = {
        "total_errors": sum(by_subject.values()),
        "mastered_errors": by_mastery.get("已掌握", 0),
        "total_practice_count": q_total,
        "total_study_hours": round(s_all / 3600, 2),
        "today_study_hours": round(s_today / 3600, 2),
        "accuracy": round(q_correct / q_total * 100, 1) if q_total else None,
    }

    # 趋势图：按天 × 科目 的正确率（3 号清单第 1 项）
    trend = [{"date": r["d"], "subject": r["code"], "rate": round(r["c"] / r["t"] * 100, 1)}
             for r in cur.execute(
                 "SELECT q.practiced_at d, s.code, SUM(q.total_count) t,"
                 " SUM(q.correct_count) c FROM quiz_records q"
                 " LEFT JOIN subjects s ON q.subject_id=s.id"
                 " WHERE q.user_id=? GROUP BY d, s.code ORDER BY d, s.code",
                 (uid,)) if r["t"]]

    # 时长图：最近 7 天，按天 × 科目 的小时数（3 号清单第 5 项）
    by_day: dict = {}
    for r in cur.execute(
            "SELECT ss.studied_at d, s.code, SUM(ss.duration_sec) sec"
            " FROM study_sessions ss LEFT JOIN subjects s ON ss.subject_id=s.id"
            " WHERE ss.user_id=? AND ss.studied_at >= date('now','localtime','-7 days')"
            " GROUP BY d, s.code ORDER BY d", (uid,)):
        day = by_day.setdefault(r["d"], {"date": r["d"]})
        day[r["code"] or "other"] = round(r["sec"] / 3600, 2)
    duration = [by_day[k] for k in sorted(by_day)]

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
            "by_question_type": by_question_type,
            "due_review": due,
            "ai_parsed": ai_done,
            "gated": gated,
            "manual": manual,
        },
        "weak_points": weak_points,
        "radar": radar,
        "summary": summary,
        "trend": trend,
        "duration": duration,
        "_missing": {
            "radar_recite": "背诵维度无数据源（系统暂无背诵功能）",
            "radar_writing": "写作维度无数据源（系统暂无写作功能）",
        },
        "_note": ("trend/duration 依赖前端上报：POST /practice/quiz 与 "
                  "POST /practice/session；当前若无上报则为空数组"),
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
