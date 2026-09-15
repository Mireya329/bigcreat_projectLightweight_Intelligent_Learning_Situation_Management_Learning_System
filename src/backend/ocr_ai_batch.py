# -*- coding: utf-8 -*-
"""AI 错题解析批处理：把库里还没解析的错题送去 AI，结果写回 analysis。

默认只跑 1 道（避免小模型一题几十秒、9 题等太久）：
    python src/backend/ocr_ai_batch.py
跑 N 道：
    python src/backend/ocr_ai_batch.py --all
    python src/backend/ocr_ai_batch.py --n 3

每跑一题都会往 ai_logs 写一条（场景/模型/耗时/字数），
这些日志就是结题材料里"AI 效果实测"的数据来源。
"""
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                 # ai_interface
sys.path.insert(0, str(HERE / "db"))          # db

import ai_interface                                # noqa: E402
from db import get_conn, DB_PATH                   # noqa: E402


def pick_limit(argv):
    """解析命令行参数：--all 跑全部，--n N 跑 N 道，默认 1 道"""
    if "--all" in argv:
        return 0                                   # 0 表示不限制
    if "--n" in argv:
        i = argv.index("--n")
        if i + 1 < len(argv):
            return int(argv[i + 1])
    return 1


def main():
    limit = pick_limit(sys.argv)

    if not ai_interface.check_ollama_alive():
        print("Ollama 不在线，先启动它（D:\\Ollama\\ollama.exe serve）")
        return
    print(f"Ollama 在线，模型: {ai_interface.MODEL_NAME}\n")

    conn = get_conn()
    cur = conn.cursor()

    sql = ("SELECT id, user_id, question_text FROM error_items"
           " WHERE analysis IS NULL ORDER BY id")
    rows = cur.execute(sql).fetchall()
    if not rows:
        print("没有待解析的错题（analysis 字段都已有内容）")
        conn.close()
        return

    todo = rows if limit == 0 else rows[:limit]
    print(f"待解析 {len(rows)} 道，本次处理 {len(todo)} 道\n")

    for r in todo:
        eid, user_id, question = r["id"], r["user_id"], r["question_text"]
        print("=" * 58)
        print(f"错题 id={eid}  题干 {len(question)} 字符")
        print("=" * 58)

        t0 = time.time()
        try:
            resp = ai_interface.explain_wrong_question(
                question=question,
                student_answer="（OCR 未识别出手写作答）")
            ok, content, err = True, resp.content, None
        except Exception as e:                      # 网络/超时/模型异常都兜住
            ok, content, err = False, "", str(e)
        elapsed = int((time.time() - t0) * 1000)

        if ok:
            cur.execute(
                "UPDATE error_items SET analysis=?, ai_model=?, ai_elapsed_ms=?"
                " WHERE id=?",
                (content, ai_interface.MODEL_NAME, elapsed, eid))
            print(f"耗时 {elapsed/1000:.1f} 秒，AI 输出 {len(content)} 字符")
            print("-" * 58)
            print(content[:600])
            if len(content) > 600:
                print(f"...（完整 {len(content)} 字符已入库）")
        else:
            print(f"调用失败: {err}")

        # 无论成功失败都记日志——失败样本同样是有价值的调优证据
        cur.execute(
            "INSERT INTO ai_logs"
            " (user_id, scene, model, error_item_id, prompt_chars,"
            "  response, elapsed_ms, ok, error_msg)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, "explain_wrong", ai_interface.MODEL_NAME, eid,
             len(question), content, elapsed, 1 if ok else 0, err))
        conn.commit()
        print()

    left = cur.execute(
        "SELECT COUNT(*) FROM error_items WHERE analysis IS NULL").fetchone()[0]
    n_log = cur.execute("SELECT COUNT(*) FROM ai_logs").fetchone()[0]
    print(f"本次完成。剩余待解析 {left} 道，ai_logs 共 {n_log} 条")
    print("数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
