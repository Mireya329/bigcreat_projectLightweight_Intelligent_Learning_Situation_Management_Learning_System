# -*- coding: utf-8 -*-
"""AI 错题解析批处理：把库里还没解析的错题送去 AI，结果写回 analysis。

默认只跑 1 道（避免小模型一题几十秒、9 题等太久）：
    python src/backend/ocr_ai_batch.py
跑 N 道：
    python src/backend/ocr_ai_batch.py --all
    python src/backend/ocr_ai_batch.py --n 3

A/B 对比用：
    python src/backend/ocr_ai_batch.py --v2 --clear --all
    --v2     使用带"OCR 残缺识别"约束的 v2 Prompt
    --clear  先清空 analysis 再跑（重跑同一批题时用）
    两套 Prompt 并存于 ai_interface.PROMPT_TEMPLATES，方便对比效果。

入库纪律（2026-09-21 队长协议）
------------------------------
* 只有 ai_interface 返回 code=0（JSON 字段校验通过）才写 error_items；
* 写入的是由 data 渲染的文本，不是模型原文；
* error_confidence < 0.5 打"待人工复核"（review_flag），error_type 不入库；
* 失败样本的模型原文只进 ai_logs（供排查），绝不进业务表。

每跑一题都会往 ai_logs 写一条（场景/模型/耗时/字数），
这些日志就是结题材料里"AI 效果实测"的数据来源。
"""
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))                 # ai_interface
sys.path.insert(0, str(HERE / "db"))          # db

import ai_interface                                # noqa: E402
from db import get_conn, DB_PATH                   # noqa: E402
from ocr_quality import score_text                 # noqa: E402


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

    USE_V2 = "--v2" in sys.argv
    # 队长协议场景名（旧名 explain_wrong* 由 ai_interface 内部做别名映射）
    SCENE = "error_analysis_v2" if USE_V2 else "error_analysis"
    SCENE_LABEL = SCENE                            # 写进 ai_logs 的场景标签
    def ASK(question, student_answer, subject):
        """按题目实际学科送 AI（协议要求 subject 必填，不能一律默认 math）"""
        return ai_interface.analyze_error(
            question=question, student_answer=student_answer,
            subject=subject, variant=("v2" if USE_V2 else None))
    # 质量门禁：低于该分的题不送 AI（见 ocr_quality.py 的设计说明）
    MIN_Q = 70
    if "--min-quality" in sys.argv:
        MIN_Q = int(sys.argv[sys.argv.index("--min-quality") + 1])

    if not ai_interface.check_ollama_alive():
        print("Ollama 不在线，先启动它（D:\\Ollama\\ollama.exe serve）")
        return
    print(f"Ollama 在线，模型: {ai_interface.MODEL_NAME}，Prompt 版本: {SCENE}\n")

    conn = get_conn()
    cur = conn.cursor()

    if "--clear" in sys.argv:                      # 重跑前清空上一版结果
        cur.execute("UPDATE error_items SET analysis=NULL, ai_model=NULL,"
                    " ai_elapsed_ms=NULL")
        conn.commit()
        print("[clear] 已清空全部 analysis，准备重跑\n")

    sql = ("SELECT e.id, e.user_id, e.question_text, s.code AS subject_code"
           " FROM error_items e LEFT JOIN subjects s ON e.subject_id=s.id"
           " WHERE e.analysis IS NULL ORDER BY e.id")
    rows = cur.execute(sql).fetchall()
    if not rows:
        print("没有待解析的错题（analysis 字段都已有内容）")
        conn.close()
        return

    todo = rows if limit == 0 else rows[:limit]
    print(f"待解析 {len(rows)} 道，本次处理 {len(todo)} 道\n")

    for r in todo:
        eid, user_id, question = r["id"], r["user_id"], r["question_text"]
        subject = ai_interface.subject_from_code(r["subject_code"])
        print("=" * 58)
        print(f"错题 id={eid}  学科 {r['subject_code']} → {subject}"
              f"  题干 {len(question)} 字符")

        # ---- 质量门禁：先体检，不合格直接拦下，不送 AI ----
        q, reasons = score_text(question)
        print(f"OCR 质量分 {q}（阈值 {MIN_Q}）")
        for x in reasons:
            print(f"   - {x}")
        if q < MIN_Q:
            cur.execute(
                "UPDATE error_items SET analysis=?, ai_model=?"
                " WHERE id=?",
                (f"[需人工确认] OCR 可信分 {q} 低于阈值 {MIN_Q}，未送 AI。"
                 f"扣分原因：{'; '.join(reasons)}", "quality_gate", eid))
            conn.commit()
            print("  🚫 被质量门禁拦下，已标记为需人工确认")
            print()
            continue
        print("  ✅ 通过门禁，送 AI 解析")
        print("=" * 58)

        t0 = time.time()
        try:
            resp = ASK(
                question=question,
                student_answer="（OCR 未识别出手写作答）",
                subject=subject)
            err = None
        except Exception as e:                      # 网络/超时/模型异常都兜住
            resp, err = None, str(e)
        elapsed = int((time.time() - t0) * 1000)

        # ---- 队长硬约束：只有 code=0（字段校验通过）才允许入库 ----
        # code=1 是降级：data 为 None，模型原文只允许留在 raw/日志里，
        # 绝不能写进 error_items.analysis。
        code = resp.code if resp is not None else 1
        ok = (code == 0)
        content = resp.content if ok else ""

        if ok:
            data = resp.data or {}
            try:
                conf = float(data.get("error_confidence", 0))
            except (TypeError, ValueError):
                conf = 0.0
            etype = data.get("error_type")
            reason = str(data.get("error_reason", ""))[:200]
            # 置信度 < 0.5 → 打"待人工复核"，error_type 不自动入库
            review = conf < ai_interface.CONFIDENCE_THRESHOLD
            cur.execute(
                "UPDATE error_items SET analysis=?, ai_model=?, ai_elapsed_ms=?,"
                " error_type=?, error_confidence=?, error_reason=?, review_flag=?"
                " WHERE id=?",
                (content, ai_interface.MODEL_NAME, elapsed,
                 None if review else etype, conf, reason,
                 "pending_review" if review else None, eid))
            print(f"耗时 {elapsed/1000:.1f} 秒，AI 输出 {len(content)} 字符")
            if review:
                print(f"  ⚠️ 置信度 {conf} < {ai_interface.CONFIDENCE_THRESHOLD}"
                      f" → 待人工复核，error_type 不入库")
            else:
                print(f"  ✅ 分类 {etype}（置信度 {conf}）已入库")
            print("-" * 58)
            print(content[:600])
            if len(content) > 600:
                print(f"...（完整 {len(content)} 字符已入库）")
        else:
            why = (resp.error if resp is not None else err) or "未知原因"
            print(f"  🚫 未入库（code={code}）：{why}")
            if resp is not None and resp.raw:
                print(f"     原文已留档到 ai_logs（{len(str(resp.raw))} 字符），"
                      f"未写入 error_items")

        # 无论成功失败都记日志——失败样本同样是有价值的调优证据
        # 日志表允许存原文（供排查），业务表不允许
        log_text = content if ok else json.dumps(
            (resp.raw if resp is not None else {"error": err}),
            ensure_ascii=False)[:4000]
        cur.execute(
            "INSERT INTO ai_logs"
            " (user_id, scene, model, error_item_id, prompt_chars,"
            "  response, elapsed_ms, ok, error_msg)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (user_id, SCENE_LABEL, ai_interface.MODEL_NAME, eid,
             len(question), log_text, elapsed, 1 if ok else 0,
             (resp.error if resp is not None else err)))
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
