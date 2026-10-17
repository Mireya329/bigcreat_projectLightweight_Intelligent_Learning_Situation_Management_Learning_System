# -*- coding: utf-8 -*-
"""后端 API 层（FastAPI）：把数据库和 AI 能力暴露成 HTTP 接口。

设计原则：
  1. **所有数据访问都走 db.get_conn()**，本文件不自己连库
  2. 写操作（POST/PATCH）复用 ocr_quality.score_text 做质量打分，保持一致口径
  3. AI 调用只允许经过 ai_interface，接口层不直连模型

启动：
    venv\\Scripts\\python.exe -m uvicorn src.backend.api:app --reload --port 8000
    （注意在项目根目录执行，模块路径 src.backend.api）

接口文档（自动生成）：
    http://127.0.0.1:8000/docs
"""
import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "db"))

from db import get_conn, DB_PATH          # noqa: E402
import ai_interface                       # noqa: E402
from ocr_quality import score_text        # noqa: E402
from study_stats import collect_stats     # noqa: E402

app = FastAPI(title="学情管理系统 API", version="0.1.0")

# 跨域：前端（Qwerty Learner / wrong-notebook 跑在 3000 等端口）直接 fetch 本服务时，
# 浏览器会先发 OPTIONS 预检并校验 CORS 头，不配置会全部被拦截。
# 本地开发阶段放开所有来源；上线前应收紧为具体域名。
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/")
def root():
    """根路径给个指引，队友打开 8000 端口不至于一脸懵"""
    return {
        "service": "学情管理系统后端 API",
        "docs": "http://127.0.0.1:8000/docs",
        "health": "http://127.0.0.1:8000/health",
        "stats": "http://127.0.0.1:8000/stats",
        "tip": "完整接口说明见 /docs，字段含义见 docs/后端字段字典.md",
    }


# ---------- 小工具 ----------
def rows_to_dicts(rows):
    return [dict(r) for r in rows]


def get_user_id(cur, username: str) -> int:
    r = cur.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
    if not r:
        raise HTTPException(404, f"用户 {username} 不存在")
    return r["id"]


# ---------- 健康检查 ----------
@app.get("/health")
def health():
    return {
        "status": "ok",
        "database": str(DB_PATH),
        "ollama_online": ai_interface.check_ollama_alive(),
        "model": ai_interface.MODEL_NAME,
    }


# ---------- 学情统计 ----------
@app.get("/stats")
def stats(username: str = "test"):
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, username)
    s = collect_stats(cur, uid)
    conn.close()
    return s


# ---------- 错题 ----------
@app.get("/errors")
def list_errors(username: str = "test", subject: Optional[str] = None,
                subject_code: Optional[str] = None,
                mastery: Optional[int] = None, limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, username)
    sql = ("SELECT e.id, e.question_text, e.source, e.mastery_level,"
           " e.ocr_quality, e.ai_model, e.error_type, e.error_type_conf,"
           " e.error_confidence, e.error_reason, e.review_flag,"
           " e.wrong_answer_text, e.answer_text,"
           " s.name AS subject,"
           " s.code AS subject_code"
           " FROM error_items e LEFT JOIN subjects s ON e.subject_id=s.id"
           " WHERE e.user_id=?")
    args = [uid]
    if subject:
        sql += " AND s.name=?"
        args.append(subject)
    if subject_code:                       # 推荐前端用 code 筛选（稳定标识）
        sql += " AND s.code=?"
        args.append(subject_code)
    if mastery is not None:
        sql += " AND e.mastery_level=?"
        args.append(mastery)
    sql += " ORDER BY e.id LIMIT ?"
    args.append(limit)
    rows = rows_to_dicts(cur.execute(sql, args).fetchall())
    conn.close()
    return {"count": len(rows), "items": rows}


@app.get("/errors/{error_id}")
def get_error(error_id: int):
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT e.*, s.name AS subject, s.code AS subject_code"
        " FROM error_items e"
        " LEFT JOIN subjects s ON e.subject_id=s.id WHERE e.id=?",
        (error_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"错题 {error_id} 不存在")
    d = dict(row)
    d["tags"] = [r["name"] for r in cur.execute(
        "SELECT t.name FROM error_item_tags et"
        " JOIN knowledge_tags t ON et.tag_id=t.id"
        " WHERE et.error_item_id=?", (error_id,)).fetchall()]
    conn.close()
    return d


class ErrorIn(BaseModel):
    username: str = "test"
    question: str
    subject: str = "考研数学"          # 五大科目之一
    subject_code: str = "POSTGRAD_MATH"
    source: str = "接口录入"
    tag: Optional[str] = None
    # 2026-10-17 补：人工补录闭环（队长第三轮回复第六节）
    # 学生原始作答 —— 没有它，AI 归因只能强制置信度 0 并交人工复核。
    # 空字符串按"没有作答"处理，不写 NULL，避免和"未知"混淆。
    answer: Optional[str] = None
    reference_answer: Optional[str] = None


@app.post("/errors")
def create_error(body: ErrorIn):
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, body.username)
    sub = cur.execute(
        "SELECT id FROM subjects WHERE user_id=? AND name=?",
        (uid, body.subject)).fetchone()
    if not sub:
        cur.execute("INSERT INTO subjects (user_id, name, code) VALUES (?,?,?)",
                    (uid, body.subject, body.subject_code))
        conn.commit()
        sub = cur.execute(
            "SELECT id FROM subjects WHERE user_id=? AND name=?",
            (uid, body.subject)).fetchone()

    quality, _ = score_text(body.question)         # 与 OCR 入库同一套打分
    cur.execute(
        "INSERT INTO error_items"
        " (user_id, subject_id, question_text, ocr_text, source,"
        "  mastery_level, ocr_quality, wrong_answer_text, answer_text)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (uid, sub["id"], body.question, body.question, body.source, 0, quality,
         (body.answer or "").strip(), (body.reference_answer or "").strip() or None))
    eid = cur.lastrowid

    if body.tag:
        t = cur.execute(
            "SELECT id FROM knowledge_tags WHERE user_id=? AND name=?",
            (uid, body.tag)).fetchone()
        if t:
            cur.execute("INSERT INTO error_item_tags VALUES (?,?)",
                        (eid, t["id"]))

    cur.execute(
        "INSERT INTO review_schedules (error_item_id, scheduled_for)"
        " VALUES (?, datetime('now','localtime','+3 day'))", (eid,))
    conn.commit()
    conn.close()
    return {"id": eid, "ocr_quality": quality,
            "answer_recorded": bool((body.answer or "").strip())}


class AnswerIn(BaseModel):
    """补录 / 修订学生作答与参考答案（人工补录闭环第二步）。"""
    answer: Optional[str] = None            # 学生原始作答
    reference_answer: Optional[str] = None  # 参考答案


@app.patch("/errors/{error_id}/answer")
def set_answer(error_id: int, body: AnswerIn):
    conn = get_conn()
    cur = conn.cursor()
    if not cur.execute("SELECT id FROM error_items WHERE id=?", (error_id,)).fetchone():
        conn.close()
        raise HTTPException(404, f"错题 {error_id} 不存在")
    before = cur.execute(
        "SELECT wrong_answer_text, answer_text FROM error_items WHERE id=?",
        (error_id,)).fetchone()
    # None 表示该字段本次不改，只改明确传了的那个
    ans = before["wrong_answer_text"] if body.answer is None else body.answer.strip()
    ref = before["answer_text"] if body.reference_answer is None \
        else (body.reference_answer.strip() or None)
    cur.execute(
        "UPDATE error_items SET wrong_answer_text=?, answer_text=?,"
        " updated_at=datetime('now','localtime') WHERE id=?",
        (ans, ref, error_id))
    conn.commit()
    conn.close()
    return {"id": error_id,
            "answer_recorded": bool(ans),
            "reference_answer_recorded": bool(ref)}


class ReviewIn(BaseModel):
    """人工复核结论（人工补录闭环第四步）。

    decision: confirmed=确认分类为真；rejected=判定 AI 归因不成立，退回待复核
    error_type 给了就以人工判断为准（覆盖 AI），不给则保留原值。
    """
    decision: str                            # confirmed / rejected
    error_type: Optional[str] = None         # 四个 code 之一，见 ai_interface
    note: Optional[str] = None


@app.patch("/errors/{error_id}/review")
def set_review(error_id: int, body: ReviewIn):
    decision = body.decision.strip().lower()
    if decision not in ("confirmed", "rejected"):
        raise HTTPException(400, "decision 只能是 confirmed / rejected")
    conn = get_conn()
    cur = conn.cursor()
    if not cur.execute("SELECT id FROM error_items WHERE id=?", (error_id,)).fetchone():
        conn.close()
        raise HTTPException(404, f"错题 {error_id} 不存在")
    if body.error_type and body.error_type not in ai_interface.ERROR_TYPE_CODES:
        conn.close()
        raise HTTPException(
            400, f"error_type 只能是：{sorted(ai_interface.ERROR_TYPE_CODES)}")
    flag = "confirmed" if decision == "confirmed" else "pending_review"
    # 注意：error_items 表里没有 error_type_label 列（label 由 error_type 现算），
    # 别在这里写它，否则 sqlite 直接报 no such column。
    if body.error_type:
        cur.execute(
            "UPDATE error_items SET review_flag=?, error_type=?,"
            " user_notes=COALESCE(?,user_notes),"
            " updated_at=datetime('now','localtime') WHERE id=?",
            (flag, body.error_type, body.note, error_id))
    else:
        cur.execute(
            "UPDATE error_items SET review_flag=?,"
            " user_notes=COALESCE(?,user_notes),"
            " updated_at=datetime('now','localtime') WHERE id=?",
            (flag, body.note, error_id))
    conn.commit()
    row = cur.execute(
        "SELECT review_flag, error_type, wrong_answer_text FROM error_items"
        " WHERE id=?", (error_id,)).fetchone()
    conn.close()
    return {"id": error_id, "review_flag": row["review_flag"],
            "error_type": row["error_type"],
            "error_type_label": ai_interface.ERROR_TYPE_CODES.get(
                row["error_type"] or "", ""),
            "answer_recorded": bool(row["wrong_answer_text"])}


@app.post("/errors/{error_id}/analyze")
def analyze_error_item(error_id: int, force: bool = False):
    """对单条错题再跑一次 AI 归因（人工补录闭环第三步）。

    ⚠️ AI 给的是**候选**错因，不是结论：
      · 结果写进 analysis 供人看，error_type **不直接入库**，
        由人工在 /review 接口确认后才算已确认分类（队长 v2.1 第二节第 4 条）
      · 已 confirmed 的记录默认不再覆盖，除非显式 force=True
      · 缺学生作答时网关会把置信度强制归零，这里如实返回，不粉饰
    """
    conn = get_conn()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT e.*, s.code AS subject_code FROM error_items e"
        " LEFT JOIN subjects s ON e.subject_id=s.id WHERE e.id=?",
        (error_id,)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"错题 {error_id} 不存在")
    if row["review_flag"] == "confirmed" and not force:
        conn.close()
        raise HTTPException(409, "该条已人工确认分类；要重跑请加 ?force=true")

    subject = ai_interface.SUBJECT_CODE_MAP.get(row["subject_code"], "math")
    resp = ai_interface.analyze_error(
        question=row["question_text"] or "",
        student_answer=row["wrong_answer_text"] or "",
        subject=subject,
        reference_answer=row["answer_text"],
    )
    if resp.code != 0 or not resp.data:
        conn.close()
        return {"id": error_id, "code": resp.code, "ok": False,
                "error": resp.error or "AI 未返回可用结果",
                "raw_reason": (resp.raw or {}).get("reason", "")}

    d = resp.data
    # 只写"给人看的解析"和置信度依据；错因本身等人工确认
    cur.execute(
        "UPDATE error_items SET analysis=?, error_confidence=?, error_reason=?,"
        " ai_model=?, ai_elapsed_ms=?, review_flag=COALESCE(review_flag,'pending_review'),"
        " updated_at=datetime('now','localtime') WHERE id=?",
        (resp.content, d.get("error_confidence"), d.get("error_reason"),
         resp.model, resp.elapsed_ms, error_id))
    conn.commit()
    conn.close()
    return {
        "id": error_id, "code": 0, "ok": True,
        "suggested_error_type": d.get("error_type"),
        "suggested_error_type_label": ai_interface.ERROR_TYPE_CODES.get(
            d.get("error_type") or "", ""),
        "error_confidence": d.get("error_confidence"),
        "answer_placeholder": bool(d.get("answer_placeholder")),
        "answer_recorded": bool(row["wrong_answer_text"]),
        "review_flag": "pending_review",
        "note": "AI 候选错因，未经人工确认不计入统计",
        "elapsed_ms": resp.elapsed_ms,
    }


@app.patch("/errors/{error_id}/mastery")
def set_mastery(error_id: int, level: int):
    if level not in (0, 1, 2):
        raise HTTPException(400, "level 只能是 0/1/2")
    conn = get_conn()
    cur = conn.cursor()
    cur.execute("UPDATE error_items SET mastery_level=? WHERE id=?",
                (level, error_id))
    conn.commit()
    conn.close()
    return {"id": error_id, "mastery_level": level}


# ---------- 待复习 ----------
@app.get("/review/due")
def due_reviews(username: str = "test"):
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, username)
    rows = rows_to_dicts(cur.execute(
        "SELECT r.id, r.error_item_id, r.scheduled_for,"
        " substr(e.question_text,1,40) AS question"
        " FROM review_schedules r JOIN error_items e ON r.error_item_id=e.id"
        " WHERE e.user_id=? AND r.completed_at IS NULL"
        " AND r.scheduled_for <= datetime('now','localtime')"
        " ORDER BY r.scheduled_for", (uid,)).fetchall())
    conn.close()
    return {"count": len(rows), "items": rows}


class QuizIn(BaseModel):
    username: str = "test"
    subject_code: str = "POSTGRAD_MATH"
    total: int
    correct: int
    duration_sec: int = 0
    practiced_at: Optional[str] = None     # YYYY-MM-DD，不传则默认今天


@app.post("/practice/quiz")
def add_quiz(body: QuizIn):
    """上报一次刷题记录（趋势图 + 正确率的数据源）。
    建议一次练习结束调用一次，不要每题一请求。"""
    if body.correct > body.total:
        raise HTTPException(400, "correct 不能大于 total")
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, body.username)
    sub = cur.execute("SELECT id FROM subjects WHERE user_id=? AND code=?",
                      (uid, body.subject_code)).fetchone()
    if body.practiced_at:
        cur.execute(
            "INSERT INTO quiz_records (user_id, subject_id, total_count,"
            " correct_count, duration_sec, practiced_at) VALUES (?,?,?,?,?,?)",
            (uid, sub["id"] if sub else None, body.total, body.correct,
             body.duration_sec, body.practiced_at))
    else:
        cur.execute(
            "INSERT INTO quiz_records (user_id, subject_id, total_count,"
            " correct_count, duration_sec) VALUES (?,?,?,?,?)",
            (uid, sub["id"] if sub else None, body.total, body.correct,
             body.duration_sec))
    conn.commit()
    conn.close()
    return {"ok": True,
            "rate": round(body.correct / body.total * 100, 1) if body.total else 0.0}


class SessionIn(BaseModel):
    username: str = "test"
    subject_code: str = "POSTGRAD_MATH"
    duration_sec: int
    studied_at: Optional[str] = None       # YYYY-MM-DD，不传则默认今天


@app.post("/practice/session")
def add_session(body: SessionIn):
    """上报一段学习计时（备考时长图的数据源）"""
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, body.username)
    sub = cur.execute("SELECT id FROM subjects WHERE user_id=? AND code=?",
                      (uid, body.subject_code)).fetchone()
    if body.studied_at:
        cur.execute(
            "INSERT INTO study_sessions (user_id, subject_id, duration_sec,"
            " studied_at) VALUES (?,?,?,?)",
            (uid, sub["id"] if sub else None, body.duration_sec, body.studied_at))
    else:
        cur.execute(
            "INSERT INTO study_sessions (user_id, subject_id, duration_sec)"
            " VALUES (?,?,?)",
            (uid, sub["id"] if sub else None, body.duration_sec))
    conn.commit()
    conn.close()
    return {"ok": True, "hours": round(body.duration_sec / 3600, 2)}


# ---------- 单词 ----------
@app.get("/words/wrong-top")
def wrong_top(username: str = "test", limit: int = 10):
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, username)
    rows = rows_to_dicts(cur.execute(
        "SELECT w.word, w.trans, SUM(r.wrong_count) AS wrong, COUNT(*) AS times"
        " FROM word_records r JOIN words w ON r.word_id=w.id"
        " WHERE r.user_id=? GROUP BY w.id"
        " HAVING wrong > 0 ORDER BY wrong DESC LIMIT ?", (uid, limit)).fetchall())
    conn.close()
    return {"count": len(rows), "items": rows}


class PracticeIn(BaseModel):
    username: str = "test"
    word: str
    dict_key: str = "CET4_T"
    wrong_count: int = 0
    total_time_ms: Optional[int] = None
    chapter: int = 0


@app.post("/words/practice")
def add_practice(body: PracticeIn):
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, body.username)
    w = cur.execute("SELECT id FROM words WHERE word=?",
                    (body.word,)).fetchone()
    cur.execute(
        "INSERT INTO word_records"
        " (user_id, word_id, dict_key, chapter, wrong_count, total_time_ms)"
        " VALUES (?,?,?,?,?,?)",
        (uid, w["id"] if w else None, body.dict_key, body.chapter,
         body.wrong_count, body.total_time_ms))
    conn.commit()
    conn.close()
    return {"word": body.word, "linked": w is not None}

# ---------- Anki 间隔重复复习 ----------
class CardIn(BaseModel):
    username: str = "test"
    front: str
    back: str = ""
    error_item_id: Optional[int] = None
    word_id: Optional[int] = None


@app.post("/anki/cards")
def create_card(body: CardIn):
    """新建一张记忆卡片"""
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, body.username)
    cur.execute(
        "INSERT INTO anki_cards (user_id, error_item_id, word_id, front, back)"
        " VALUES (?,?,?,?,?)",
        (uid, body.error_item_id, body.word_id, body.front, body.back))
    conn.commit()
    cid = cur.lastrowid
    conn.close()
    return {"id": cid}


@app.get("/anki/due")
def due_cards(username: str = "test", limit: int = 20):
    """今日到期待复习的卡片"""
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, username)
    rows = rows_to_dicts(cur.execute(
        "SELECT id, front, back, ef, interval_days, repetitions, due_date"
        " FROM anki_cards WHERE user_id=? AND due_date <= date('now','localtime')"
        " ORDER BY due_date LIMIT ?", (uid, limit)).fetchall())
    conn.close()
    return {"count": len(rows), "items": rows}


class ReviewIn(BaseModel):
    card_id: int
    quality: int                       # 0~5
    username: str = "test"


@app.post("/anki/review")
def review_card(body: ReviewIn):
    """提交一次复习结果，按 SM-2 更新卡片并返回下次到期日"""
    # 先校验：不校验的话 quality=9 会让内部抛 ValueError，
    # 接口直接 500 且返回空内容，前端拿不到任何提示（实测踩过）
    if not 0 <= body.quality <= 5:
        raise HTTPException(400, "quality 必须在 0~5 之间")
    from datetime import date
    from anki_sm2 import review as sm2_review      # 复用第 2 步的纯函数

    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, body.username)
    row = cur.execute(
        "SELECT * FROM anki_cards WHERE id=? AND user_id=?",
        (body.card_id, uid)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, f"卡片 {body.card_id} 不存在")

    r = sm2_review(row["ef"], row["interval_days"], row["repetitions"],
                   body.quality, date.today())
    cur.execute(
        "UPDATE anki_cards SET ef=?, interval_days=?, repetitions=?,"
        " due_date=?, last_reviewed_at=datetime('now','localtime') WHERE id=?",
        (r["ef"], r["interval_days"], r["repetitions"], r["due_date"],
         body.card_id))
    conn.commit()
    conn.close()
    return {"card_id": body.card_id, **r}
