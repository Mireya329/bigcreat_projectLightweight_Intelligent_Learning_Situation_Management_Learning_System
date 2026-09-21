# -*- coding: utf-8 -*-
"""API 冒烟测试：把每个接口都真打一遍，验证能通、返回对。

用法（先启动服务）：
    venv\\Scripts\\python.exe -m uvicorn src.backend.api:app --port 8000
    venv\\Scripts\\python.exe src/backend/api_smoke_test.py

自动清理
--------
冒烟测试会真写库（新增错题、上报刷题/计时、复习卡片），跑完如果不清理，
统计接口的数字就被测试数据污染了——这个问题踩过一次。
所以脚本在开始前拍快照，结束前把库恢复原样。
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn, DB_PATH          # noqa: E402

BASE = "http://127.0.0.1:8000"

# 只增不删的表：跑完删掉 id 大于基线的行
APPEND_ONLY = ("error_items", "quiz_records", "study_sessions")
# 会被改写的表：整行快照，跑完原样写回
ROW_SNAPSHOT = ("anki_cards", "word_records")


def call(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def show(title, status, data, keys=None):
    ok = "✅" if 200 <= status < 300 else "❌"
    print(f"{ok} {title}  HTTP {status}")
    if keys and isinstance(data, dict):
        print("   ", {k: data.get(k) for k in keys})
    elif isinstance(data, dict) and "count" in data:
        print(f"    count={data['count']}")
        for it in data.get("items", [])[:2]:
            s = json.dumps(it, ensure_ascii=False)
            print("     ", s[:90])
    else:
        print("   ", json.dumps(data, ensure_ascii=False)[:110])


def snapshot():
    """跑测试前的库快照：只增表的当前最大 id + 会被改写的行原值"""
    conn = get_conn()
    cur = conn.cursor()
    snap = {"max_id": {}, "rows": {}}
    for t in APPEND_ONLY:
        snap["max_id"][t] = cur.execute(f"SELECT MAX(id) FROM {t}").fetchone()[0] or 0
    for t in ROW_SNAPSHOT:
        snap["rows"][t] = [dict(r) for r in cur.execute(f"SELECT * FROM {t}")]
    conn.close()
    return snap


def cleanup(snap):
    """把库恢复成跑测试前的样子"""
    conn = get_conn()
    cur = conn.cursor()
    for t, mid in snap["max_id"].items():
        n = cur.execute(f"DELETE FROM {t} WHERE id > ?", (mid,)).rowcount
        if n:
            print(f"   [清理] {t} 删除 {n} 行测试数据")
    for t, rows in snap["rows"].items():
        fixed = 0
        for r in rows:
            sets = ", ".join(f"{k}=?" for k in r if k != "id")
            vals = [r[k] for k in r if k != "id"] + [r["id"]]
            cur.execute(f"UPDATE {t} SET {sets} WHERE id=?", vals)
            fixed += cur.rowcount
        if fixed:
            print(f"   [清理] {t} 还原 {fixed} 行")
    conn.commit()
    conn.close()
    print(f"   [清理] 完成，数据库已复原：{DB_PATH}")


def main():
    print("=" * 58)
    print("API 冒烟测试")
    print("=" * 58)
    snap = snapshot()

    s, d = call("GET", "/health")
    show("健康检查", s, d, ["status", "ollama_online", "model"])

    s, d = call("GET", "/stats?username=test")
    show("学情统计", s, d, ["word", "error"])

    s, d = call("GET", "/errors?limit=3")
    show("错题列表", s, d)

    s, d = call("GET", "/errors/12")
    show("错题详情(含标签)", s, d, ["id", "subject", "ocr_quality", "tags"])

    s, d = call("POST", "/errors", {
        "question": "设 z = x^2 y + sin(xy)，求 ∂z/∂x 与 ∂z/∂y。",
        "subject": "考研数学", "source": "接口冒烟测试", "tag": "偏导数与高阶偏导数"})
    show("新增错题", s, d)
    new_id = d.get("id") if isinstance(d, dict) else None

    if new_id:
        s, d = call("PATCH", f"/errors/{new_id}/mastery?level=1")
        show("更新掌握度", s, d)

    s, d = call("GET", "/words/wrong-top?limit=3")
    show("易错词 TOP", s, d)

    s, d = call("POST", "/words/practice", {
        "word": "carrier", "wrong_count": 2, "total_time_ms": 1800})
    show("上报练习记录", s, d)

    s, d = call("POST", "/practice/quiz", {
        "subject_code": "POSTGRAD_MATH", "total": 20, "correct": 15,
        "duration_sec": 1800})
    show("上报刷题记录", s, d)

    s, d = call("POST", "/practice/session", {
        "subject_code": "POSTGRAD_MATH", "duration_sec": 3600})
    show("上报学习计时", s, d)

    s, d = call("GET", "/review/due")
    show("待复习列表", s, d)

    print("\n--- Anki 间隔重复 ---")
    s, d = call("GET", "/anki/due?limit=3")
    show("今日到期卡片", s, d)

    first = (d.get("items") or [{}])[0].get("id") if isinstance(d, dict) else None
    if first:
        s, d = call("POST", "/anki/review", {"card_id": first, "quality": 4})
        show(f"复习卡片 {first}(q=4)", s, d)
        s, d = call("POST", "/anki/review", {"card_id": first, "quality": 9})
        print(f"{'✅' if s == 400 else '❌'} 非法 quality=9 应返回 400  HTTP {s}")

    print("\n全部接口测试完成")

    print("\n--- 清理测试数据 ---")
    cleanup(snap)


if __name__ == "__main__":
    main()
