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
                mastery: Optional[int] = None, limit: int = 50):
    conn = get_conn()
    cur = conn.cursor()
    uid = get_user_id(cur, username)
    sql = ("SELECT e.id, e.question_text, e.source, e.mastery_level,"
           " e.ocr_quality, e.ai_model, s.name AS subject"
           " FROM error_items e LEFT JOIN subjects s ON e.subject_id=s.id"
           " WHERE e.user_id=?")
    args = [uid]
    if subject:
        sql += " AND s.name=?"
        args.append(subject)
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
        "SELECT e.*, s.name AS subject FROM error_items e"
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
    subject: str = "数学"
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
        cur.execute("INSERT INTO subjects (user_id, name) VALUES (?,?)",
                    (uid, body.subject))
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
