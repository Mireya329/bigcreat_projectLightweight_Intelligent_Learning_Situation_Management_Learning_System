# -*- coding: utf-8 -*-
"""API 冒烟测试：把每个接口都真打一遍，验证能通、返回对。

用法（先启动服务）：
    venv\\Scripts\\python.exe -m uvicorn src.backend.api:app --port 8000
    venv\\Scripts\\python.exe src/backend/api_smoke_test.py
"""
import json
import urllib.request
import urllib.error

BASE = "http://127.0.0.1:8000"


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


def main():
    print("=" * 58)
    print("API 冒烟测试")
    print("=" * 58)

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

    s, d = call("GET", "/review/due")
    show("待复习列表", s, d)

    print("\n全部接口测试完成")


if __name__ == "__main__":
    main()
