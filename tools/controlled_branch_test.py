# -*- coding: utf-8 -*-
"""受控链路复测：占位符与空答案的五个分支（队长 v2.2 第五节）。

做法：**不随机调用 7B**。用一个脚本化的 transport 顶替 Ollama，
模型返回什么由用例写死，然后走真实链路：
    后端 analyze_error  →  HTTP 网关（v2.2，in-process） →  校验/兜底/重试
这样每次跑出来的结果完全可复现，还能数清调用次数、看到 raw 里保留的原文。

覆盖的分支（第五节建议）：
  1 空答案 + 末步有明确结论  → 补全，code=0
  2 空答案 + 末步无结论      → 重试 1 次后失败，code=1，data=null
  3 占位符连续两次           → 失败，raw 指出占位文本，无可用 data
  4 首次占位符、第二次真答案 → 恢复，code=0，且保留第一次失败记录
  5 嵌套字段（solution）占位 → 同样拒绝；第二次给真值则恢复
  6 失败结果不得产生业务写入（走 API 验证库里 analysis 没被改）

用法：
  LEARNING_DB_PATH=<测试库> python tools/controlled_branch_test.py
  python tools/controlled_branch_test.py --db data/learning_test_v22.db

⚠️ 2026-10-21 修（队长第五轮第六节 + Q01 交接第四节指出）：
原来后半段把 LEARNING_DB_PATH 重新设成固定的 data/learning_test_v22.db，
等于前段读环境变量、后段又改回硬编码，两套配置。现在只有一处生效：
--db  >  环境变量  >  默认测试库，并且在任何写库检查前打印最终绝对路径。
"""
import argparse
import json
import os
import sys
import threading
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src", "backend"))
sys.path.insert(0, os.path.join(ROOT, "vendor", "captain_ai_v2"))
sys.path.insert(0, os.path.join(ROOT, "src", "backend", "db"))

_ap = argparse.ArgumentParser()
_ap.add_argument("--db", default=None, help="测试库路径（覆盖环境变量）")
_args, _ = _ap.parse_known_args()

# 单一配置：--db 优先，其次环境变量，最后默认测试库。
# ⚠️ 必须在这里一次性定好，后面任何地方都不许再改（改了就是两套配置）。
DEFAULT_TEST_DB = os.path.join(ROOT, "data", "learning_test_v22.db")
if _args.db:
    os.environ["LEARNING_DB_PATH"] = _args.db
else:
    os.environ.setdefault("LEARNING_DB_PATH", DEFAULT_TEST_DB)
os.environ["AI_PROVIDER"] = "gateway"          # 必须在 import ai_interface 之前

import captain_ai                               # noqa: E402
import server as captain_server                # noqa: E402
import ai_interface                            # noqa: E402

PORT = 8799
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(f"  {'✓' if cond else '✗'} {name}" + (f"  {detail}" if detail else ""))


class ScriptedTransport:
    """按脚本返回模型原文；用完了就重复最后一条（模拟模型一直给同样的坏输出）。"""

    def __init__(self):
        self.script = []
        self.calls = []

    def load(self, *responses):
        self.script = list(responses)
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        if not self.script:
            raise RuntimeError("脚本已耗尽")
        return self.script.pop(0) if len(self.script) > 1 else self.script[0]


transport = ScriptedTransport()


def base_data(**kw):
    d = {"answer": "2e+1", "solution": ["先求偏导", "代入 x=1,y=1 得 2e+1"],
         "knowledge_points": ["偏导数"], "error_type": "calculation_error",
         "error_type_label": "计算失误", "error_confidence": 0.8,
         "error_reason": "代入时符号写错", "suggestions": ["再练两道"]}
    d.update(kw)
    return d


def j(d):
    return json.dumps(d, ensure_ascii=False)


# ---------- 起一个可控网关 ----------
client = captain_ai.Client(model="scripted-transport", transport=transport)
httpd = captain_server.make_server(PORT, client=client)
threading.Thread(target=httpd.serve_forever, daemon=True).start()
ai_interface.AI_GATEWAY_URL = f"http://127.0.0.1:{PORT}/ai"
print(f"受控网关已启动 127.0.0.1:{PORT}（模型响应由脚本固定，不调真实 7B）")

QUESTION = "设 f(x,y)=x^2*e^y，求 f 对 x 的偏导数在 (1,1) 处的值"
STUDENT = "我算的是 2e"


def run(student_answer=STUDENT):
    """跑一次 analyze_error，返回 (响应对象, 调用次数)"""
    transport.calls = []
    resp = ai_interface.analyze_error(question=QUESTION,
                                      student_answer=student_answer,
                                      subject="math")
    return resp, len(transport.calls)


print()
print("=" * 68)
print("分支 1：空答案 + 末步有明确结论 → 应补全")
transport.load(j(base_data(answer="",
                           solution=["先求导", "最终答案：2e+1"])))
r, calls = run()
check("code=0", r.code == 0, f"code={r.code} err={r.error}")
check("补全出真实答案", (r.data or {}).get("answer") == "2e+1",
      repr((r.data or {}).get("answer")))
check("补全记进 raw.normalizations",
      "normalizations" in (r.raw or {}), str(list((r.raw or {}).keys())))
check("只调用 1 次（一次就过）", calls == 1, f"calls={calls}")

print()
print("分支 2：空答案 + 末步无结论 → 重试后失败")
transport.load(j(base_data(answer="",
                           solution=["先求导", "代入 x=1,y=1 得到结果"])))
r, calls = run()
check("code=1", r.code == 1, f"code={r.code}")
check("data 为 None（无可用结果）", r.data is None, repr(r.data)[:80])
check("重试了 1 次", (r.raw or {}).get("retried") == 1, str((r.raw or {}).get("retried")))
check("调用 2 次（首次 + 重试）", calls == 2, f"calls={calls}")
check("raw 保留失败原因", "empty text" in str((r.raw or {}).get("reason", "")),
      str((r.raw or {}).get("reason"))[:80])
check("raw 保留模型原文", bool((r.raw or {}).get("text")))
check("content 为空串（不得渲染出去）", r.content == "", repr(r.content)[:60])

print()
print("分支 3：占位符连续两次 → 失败且无 data")
transport.load(j(base_data(answer="请填写分析结果")))
r, calls = run()
check("code=1", r.code == 1, f"code={r.code}")
check("data 为 None", r.data is None)
check("raw 指出是占位文本",
      "placeholder" in str((r.raw or {}).get("reason", "")).lower(),
      str((r.raw or {}).get("reason"))[:100])
check("调用 2 次", calls == 2, f"calls={calls}")
check("有作答时也不把占位符当答案（本例有作答，仍失败）", r.data is None)

print()
print("分支 4：首次占位符、第二次真答案 → 恢复")
transport.load(j(base_data(answer="请填写分析结果")),
               j(base_data(answer="2e+1")))
r, calls = run()
check("code=0（第二次恢复）", r.code == 0, f"code={r.code} err={r.error}")
check("拿到真实答案", (r.data or {}).get("answer") == "2e+1",
      repr((r.data or {}).get("answer")))
check("调用 2 次", calls == 2, f"calls={calls}")
check("保留第一次失败记录",
      len((r.raw or {}).get("attempts", [])) == 1,
      str((r.raw or {}).get("attempts"))[:120])
check("raw 标记 retried=1", (r.raw or {}).get("retried") == 1)

print()
print("分支 5：嵌套字段（solution 某一项）占位符 → 拒绝")
transport.load(j(base_data(solution=["先求导", "请填写分析结果"])))
r, calls = run()
check("code=1", r.code == 1, f"code={r.code}")
check("data 为 None", r.data is None)
check("拒因指向嵌套位置",
      "solution" in str((r.raw or {}).get("reason", "")),
      str((r.raw or {}).get("reason"))[:100])

print()
print("分支 5b：嵌套占位 → 第二次给真值，应恢复")
transport.load(j(base_data(solution=["先求导", "请填写分析结果"])),
               j(base_data(solution=["先求导", "代入得 2e+1"])))
r, calls = run()
check("code=0", r.code == 0, f"code={r.code}")
check("solution 是真步骤", (r.data or {}).get("solution") ==
      ["先求导", "代入得 2e+1"], str((r.data or {}).get("solution")))

print()
print("分支 6：缺作答 → 置信度强制归零（降级，不是失败）")
transport.load(j(base_data()))
r, calls = run(student_answer="")
check("code=0（降级不是失败）", r.code == 0, f"code={r.code}")
check("置信度被强制为 0", (r.data or {}).get("error_confidence") == 0.0,
      str((r.data or {}).get("error_confidence")))
check("原置信度保留在 raw.model_error_confidence",
      (r.raw or {}).get("model_error_confidence") == 0.8,
      str((r.raw or {}).get("model_error_confidence")))
check("raw 标记 answer_evidence=missing",
      (r.raw or {}).get("answer_evidence") == "missing")

print()
print("分支 7：合法枚举值不得被误伤（占位符只管自由文本）")
transport.load(j(base_data(answer="2e+1", error_type="concept_misunderstanding",
                           error_type_label="概念不清")))
r, calls = run()
check("code=0", r.code == 0, f"code={r.code}")
check("concept_misunderstanding 未被当成占位符拒绝",
      (r.data or {}).get("error_type") == "concept_misunderstanding",
      str((r.data or {}).get("error_type")))

# ---------- 失败结果不得产生业务写入 ----------
print()
print("业务写入检查：失败分支不得改动库里的解析")
os.environ["AI_PROVIDER"] = "gateway"
os.environ["AI_GATEWAY_URL"] = f"http://127.0.0.1:{PORT}/ai"
import api as backend_api                                   # noqa: E402
import db as dbmod                                          # noqa: E402

# 写库（哪怕是"检查没被写"）之前，先把最终绝对路径打印出来核对
dbmod.announce_db(role="受控分支测试")
conn = dbmod.get_conn()
cur = conn.cursor()
row = cur.execute(
    "SELECT id, analysis, error_confidence, review_flag FROM error_items"
    " WHERE id=(SELECT MIN(id) FROM error_items)").fetchone()
target = row["id"]
before = (row["analysis"], row["error_confidence"], row["review_flag"])
conn.close()
print(f"  取 id={target}，改前 analysis={str(before[0])[:30]!r}")

transport.load(j(base_data(answer="请填写分析结果")))
from fastapi.testclient import TestClient                    # noqa: E402
tc = TestClient(backend_api.app)
resp = tc.post(f"/errors/{target}/analyze")
body = resp.json()
check("API 返回 ok=false", body.get("ok") is False, json.dumps(body, ensure_ascii=False)[:160])
check("API 给出失败原因", bool(body.get("error") or body.get("raw_reason")),
      str(body.get("raw_reason"))[:80])

conn = dbmod.get_conn()
cur = conn.cursor()
after = cur.execute(
    "SELECT analysis, error_confidence, review_flag FROM error_items WHERE id=?",
    (target,)).fetchone()
conn.close()
check("库里 analysis 未被改动", (after["analysis"],) == (before[0],),
      f"{str(before[0])[:25]!r} -> {str(after['analysis'])[:25]!r}")
check("库里置信度未被改动", after["error_confidence"] == before[1])
check("库里状态未被改动", after["review_flag"] == before[2])

httpd.shutdown()
print()
print("=" * 68)
print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print("   ✗ " + f)
    raise SystemExit(1)
print("✅ 受控链路五个分支全部符合预期")
