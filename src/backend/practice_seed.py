# -*- coding: utf-8 -*-
"""刷题记录 / 学习计时 的演示数据生成器。

⚠️ **生成的是演示数据，不是真实学习记录**。
用途：让 3 号前端能立刻看到趋势图和时长图长什么样。
真实数据应由前端在做题/学习结束时调用 POST /practice/quiz 与 POST /practice/session 上报。

用法：
    python src/backend/practice_seed.py            # 默认造 14 天
    python src/backend/practice_seed.py --days 30
"""
import random
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn, DB_PATH          # noqa: E402

USERNAME = "test"
# 各科权重：数学和英语练得多，政治少一些
WEIGHT = {"POSTGRAD_MATH": 0.3, "POSTGRAD_ENGLISH": 0.25,
          "CET4": 0.2, "CET6": 0.15, "POSTGRAD_POLITICS": 0.1}


def main():
    days = 14
    if "--days" in sys.argv:
        days = int(sys.argv[sys.argv.index("--days") + 1])
    random.seed(42)                        # 固定种子，结果可复现

    conn = get_conn()
    cur = conn.cursor()
    uid = cur.execute(
        "SELECT id FROM users WHERE username=?", (USERNAME,)).fetchone()[0]
    subs = {r["code"]: r["id"] for r in cur.execute(
        "SELECT id, code FROM subjects WHERE user_id=? AND code IS NOT NULL",
        (uid,))}

    # 可重复运行：先清掉这个用户的旧记录
    cur.execute("DELETE FROM quiz_records WHERE user_id=?", (uid,))
    cur.execute("DELETE FROM study_sessions WHERE user_id=?", (uid,))

    nq = ns = 0
    today = date.today()
    for i in range(days - 1, -1, -1):
        d = today - timedelta(days=i)
        if random.random() < 0.15:         # 偶尔休息一天，更真实
            continue
        for code, w in WEIGHT.items():
            if code not in subs:
                continue
            if random.random() > w * 3:    # 不是每天每科都练
                continue
            total = random.randint(8, 30)
            correct = int(total * random.uniform(0.45, 0.95))
            dur = random.randint(600, 3600)
            cur.execute(
                "INSERT INTO quiz_records (user_id, subject_id, total_count,"
                " correct_count, duration_sec, practiced_at) VALUES (?,?,?,?,?,?)",
                (uid, subs[code], total, correct, dur, d.isoformat()))
            nq += 1
            cur.execute(
                "INSERT INTO study_sessions (user_id, subject_id, duration_sec,"
                " studied_at) VALUES (?,?,?,?)",
                (uid, subs[code], random.randint(1200, 5400), d.isoformat()))
            ns += 1
    conn.commit()

    print(f"[演示数据] 已生成 quiz_records {nq} 条、study_sessions {ns} 条（近 {days} 天）")
    print("⚠️ 这是演示数据，真实数据请由前端上报\n")

    for r in cur.execute(
            "SELECT practiced_at d, SUM(total_count) t, SUM(correct_count) c"
            " FROM quiz_records WHERE user_id=? GROUP BY d ORDER BY d DESC LIMIT 5",
            (uid,)):
        rate = r["c"] / r["t"] * 100
        print(f"  {r['d']}  做 {r['t']:>3} 题，对 {r['c']:>3}，正确率 {rate:.1f}%")
    print("\n数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
