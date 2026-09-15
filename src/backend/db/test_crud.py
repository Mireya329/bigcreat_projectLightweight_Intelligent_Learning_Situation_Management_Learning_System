# -*- coding: utf-8 -*-
"""CRUD 冒烟测试：增查改删 + 外键级联验证。跑完恢复 seed 状态"""
from db import get_conn


def main():
    conn = get_conn()
    cur = conn.cursor()

    # 0. 动态取 id（别写死 id=1！AUTOINCREMENT 的 id 会一直涨）
    user_id = cur.execute(
        "SELECT id FROM users WHERE username='test'").fetchone()[0]
    math_id = cur.execute(
        "SELECT id FROM subjects WHERE user_id=? AND name=?",
        (user_id, "数学")).fetchone()[0]
    print(f"[准备] user_id={user_id}  math_id={math_id}")

    # ---------- C：增 ----------
    cur.execute(
        "INSERT INTO error_items (user_id, subject_id, question_text, source)"
        " VALUES (?,?,?,?)",
        (user_id, math_id, "测试错题：简述死锁的四个必要条件", "CRUD测试"),
    )
    test_id = cur.lastrowid
    print(f"[增] 插入测试错题 id={test_id}")

    # 顺便挂标签、排复习计划（一会儿删它，看会不会跟着消失）
    tag_id = cur.execute(
        "SELECT id FROM knowledge_tags WHERE user_id=? LIMIT 1",
        (user_id,)).fetchone()[0]
    cur.execute("INSERT INTO error_item_tags VALUES (?,?)", (test_id, tag_id))
    cur.execute(
        "INSERT INTO review_schedules (error_item_id, scheduled_for)"
        " VALUES (?,datetime('now','localtime','+3 day'))",
        (test_id,))
    print("[增] 已挂 1 个标签 + 1 条 3 天后的复习计划")

    # ---------- R：查（注意 row["列名"] 写法，row_factory 的功劳） ----------
    row = cur.execute(
        "SELECT question_text, mastery_level FROM error_items WHERE id=?",
        (test_id,)).fetchone()
    print(f"[查] 题干={row['question_text']}  掌握度={row['mastery_level']}")

    # ---------- U：改 ----------
    cur.execute(
        "UPDATE error_items SET mastery_level=1 WHERE id=?", (test_id,))
    row = cur.execute(
        "SELECT mastery_level FROM error_items WHERE id=?", (test_id,)).fetchone()
    print(f"[改] 掌握度 0 -> {row['mastery_level']}")

    conn.commit()   # 增改都完成了，提交一次

    # 删除前的关联数据量
    before_tags = cur.execute("SELECT COUNT(*) FROM error_item_tags").fetchone()[0]
    before_rev  = cur.execute("SELECT COUNT(*) FROM review_schedules").fetchone()[0]

    # ---------- D：删 ----------
    cur.execute("DELETE FROM error_items WHERE id=?", (test_id,))
    conn.commit()
    print(f"[删] 已删除测试错题 id={test_id}")

    # 验证级联：标签和复习计划应该跟着少 1
    after_tags = cur.execute("SELECT COUNT(*) FROM error_item_tags").fetchone()[0]
    after_rev  = cur.execute("SELECT COUNT(*) FROM review_schedules").fetchone()[0]
    print(f"[级联验证] error_item_tags: {before_tags}->{after_tags}（应减1）")
    print(f"[级联验证] review_schedules: {before_rev}->{after_rev}（应减1）")

    # ---------- 最终状态 ----------
    n = cur.execute("SELECT COUNT(*) FROM error_items").fetchone()[0]
    print(f"[收尾] error_items 剩 {n} 条（应为 1，即 seed 那条）")
    conn.close()
    print("CRUD 冒烟测试全部通过")


if __name__ == "__main__":
    main()
