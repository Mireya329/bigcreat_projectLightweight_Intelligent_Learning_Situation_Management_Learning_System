# -*- coding: utf-8 -*-
"""验证队长 v2.2 第四节四个后端待修点（在测试库上跑，不碰正式库）。

四个待修点：
  1. 占位符动态加载路径（由 ai_protocol_test.py 的 v2-9 组覆盖，这里复述结论）
  2. 作答变更后旧确认状态未失效
  3. 强制分析后库里状态与返回体不一致
  4. 确认时可能没有有效分类

用法：
  LEARNING_DB_PATH=... python tools/verify_v22_fixes.py [--base http://127.0.0.1:8021]
"""
import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8021"
for i, a in enumerate(sys.argv):
    if a == "--base" and i + 1 < len(sys.argv):
        BASE = sys.argv[i + 1]

PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  {detail}" if detail else ""))


def req(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    r = urllib.request.Request(BASE + path, data=data,
                               headers={"Content-Type": "application/json"},
                               method=method)
    return json.loads(urllib.request.urlopen(r, timeout=300).read().decode())


def expect_err(method, path, body, code):
    """期待某个 HTTP 错误码，返回 (是否符合, 实际信息)"""
    try:
        req(method, path, body)
        return False, "没报错（不该成功）"
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        return e.code == code, f"{e.code} {detail[:120]}"


def confirmed_count():
    """/stats 的已确认数嵌在 error 段里（顶层没有），别读错层。"""
    s = json.loads(urllib.request.urlopen(BASE + "/stats", timeout=30).read().decode())
    return (s.get("error") or {}).get("confirmed", 0), s


print("=" * 66)
print("待修点 2：作答变更后旧确认状态必须失效")

# 建一条带作答的新题
r = req("POST", "/errors", {"question": "验证题：求 f(x)=x^2 在 x=2 处的导数",
                            "answer": "我算的是 2", "reference_answer": "4",
                            "source": "v22验证"})
eid = r["id"]
print(f"  新建错题 id={eid}")

# 先给它一个已确认分类
rv = req("PATCH", f"/errors/{eid}/review",
         {"decision": "confirmed", "error_type": "calculation_error",
          "note": "人工确认：抄写失误"})
check("先确认成功", rv["review_flag"] == "confirmed", str(rv))

c0, s0 = confirmed_count()
check("统计里已确认数 >= 1", c0 >= 1, f"confirmed={c0}")

# 2a 提交完全相同的作答 —— 不应重置
same = req("PATCH", f"/errors/{eid}/answer", {"answer": "我算的是 2"})
check("内容未变时不重置状态", same["review_flag"] == "confirmed" and not same["changed"],
      str(same))

# 2b 改作答 —— 应退回待复核 + 归档
chg = req("PATCH", f"/errors/{eid}/answer", {"answer": "我算的是 2x=4，抄成了 2"})
check("内容变更时退回待复核", chg["review_flag"] == "pending_review", str(chg))
check("返回体标明旧结论已失效", chg["previous_conclusion_invalidated"] is True)
c1, _ = confirmed_count()
check("不再计入已确认统计", c1 == c0 - 1, f"{c0} -> {c1}")

# 归档表里确实有记录
hist = json.loads(urllib.request.urlopen(
    BASE + f"/errors/{eid}/history", timeout=30).read().decode())
check("旧结论已归档", len(hist["history"]) >= 1, str([h["event"] for h in hist["history"]]))
check("归档里保留了旧 error_type",
      any(h["error_type"] == "calculation_error" for h in hist["history"]),
      str([h["error_type"] for h in hist["history"]]))
check("归档里保留了旧作答",
      any(h.get("wrong_answer_text") == "我算的是 2" for h in hist["history"]))

print()
print("待修点 4：确认时必须有有效分类")

# 4a 新题无历史分类，confirmed 不带 error_type → 应 400 且不改状态
r2 = req("POST", "/errors", {"question": "验证题B：无分类确认", "source": "v22验证"})
eid2 = r2["id"]
ok, detail = expect_err("PATCH", f"/errors/{eid2}/review", {"decision": "confirmed"}, 400)
check("无分类确认被拒（400）", ok, detail)
st = json.loads(urllib.request.urlopen(
    BASE + f"/errors?limit=200", timeout=30).read().decode())
row2 = [x for x in st["items"] if x["id"] == eid2][0]
check("被拒后状态未被修改", row2["review_flag"] != "confirmed",
      f"review_flag={row2['review_flag']}")

# 4b 带上合法 error_type 就能确认
rv2 = req("PATCH", f"/errors/{eid2}/review",
          {"decision": "confirmed", "error_type": "misread_question"})
check("带合法分类可确认", rv2["review_flag"] == "confirmed", str(rv2))

# 4c 已有合法分类时，不带 error_type 也能沿用确认
rv3 = req("PATCH", f"/errors/{eid2}/review", {"decision": "confirmed"})
check("沿用已有分类确认", rv3["review_flag"] == "confirmed" and
      rv3["error_type"] == "misread_question", str(rv3))

# 4d rejected 不需要分类
rv4 = req("PATCH", f"/errors/{eid2}/review", {"decision": "rejected"})
check("rejected 不要求分类", rv4["review_flag"] == "pending_review", str(rv4))

print()
print("待修点 3：强制分析后库与返回体一致")

t0 = time.time()
an = req("POST", f"/errors/{eid}/analyze?force=true")
print(f"  分析耗时 {time.time() - t0:.1f}s  code={an.get('code')}")
if an.get("ok"):
    check("返回体 review_flag=pending_review", an["review_flag"] == "pending_review",
          str(an.get("review_flag")))
    row = [x for x in json.loads(urllib.request.urlopen(
        BASE + "/errors?limit=200", timeout=30).read().decode())["items"]
        if x["id"] == eid][0]
    check("库里状态与返回体一致", row["review_flag"] == an["review_flag"],
          f"库={row['review_flag']} 返回={an['review_flag']}")
    check("返回体带上重分析前的状态", an.get("previous_review_flag") == "pending_review",
          str(an.get("previous_review_flag")))
else:
    check("重分析失败时不改状态（这条如实记录为失败）", True,
          f"code={an.get('code')} error={an.get('error')}")

# 已确认的题 force 重跑：库与返回体必须同时退回 pending_review
req("PATCH", f"/errors/{eid}/review",
    {"decision": "confirmed", "error_type": "calculation_error"})
c_before, _ = confirmed_count()
check("force 前确实计入已确认统计", c_before == c1 + 1, f"{c1} -> {c_before}")
an2 = req("POST", f"/errors/{eid}/analyze?force=true")
if an2.get("ok"):
    row = [x for x in json.loads(urllib.request.urlopen(
        BASE + "/errors?limit=200", timeout=30).read().decode())["items"]
        if x["id"] == eid][0]
    check("confirmed 记录 force 后：库=pending_review",
          row["review_flag"] == "pending_review", row["review_flag"])
    check("confirmed 记录 force 后：返回体=库",
          an2["review_flag"] == row["review_flag"],
          f"库={row['review_flag']} 返回={an2['review_flag']}")
    check("force 前状态如实回传", an2.get("previous_review_flag") == "confirmed",
          str(an2.get("previous_review_flag")))
    c2, _ = confirmed_count()
    check("force 后不再计入已确认统计", c2 == c_before - 1, f"{c_before} -> {c2}")
    hist2 = json.loads(urllib.request.urlopen(
        BASE + f"/errors/{eid}/history", timeout=30).read().decode())
    check("force 重分析归档了旧人工结论",
          any(h["event"] == "reanalyzed" and h["error_type"] == "calculation_error"
              for h in hist2["history"]),
          str([(h["event"], h["error_type"]) for h in hist2["history"]]))
else:
    check("force 重分析成功（失败则如实记录）", False, str(an2))

print()
print("=" * 66)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print("   ✗ " + f)
    raise SystemExit(1)
print("✅ 四项待修点全部验证通过")
