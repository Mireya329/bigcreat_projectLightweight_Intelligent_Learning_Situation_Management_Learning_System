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
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "db"))

from db import get_conn, DB_PATH          # noqa: E402
import ai_interface                       # noqa: E402
from ocr_quality import score_text        # noqa: E402
from study_stats import collect_stats     # noqa: E402

app = FastAPI(title="学情管理系统 API", version="0.1.0")


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
        "  mastery_level, ocr_quality)"
        " VALUES (?,?,?,?,?,?,?)",
        (uid, sub["id"], body.question, body.question, body.source, 0, quality))
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
    return {"id": eid, "ocr_quality": quality}


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
