# -*- coding: utf-8 -*-
"""真实打一次队长网关：先打不调模型的分支，再打需要模型的分支。"""
import os
import sys

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "vendor", "captain_ai_v2")
sys.path.insert(0, PKG)
os.environ["AI_GATEWAY_TIMEOUT"] = "90"        # 本机只有 0.5b，别等太久

from backend_contract import call_gateway, validate_response, storage_route  # noqa: E402

print("网关地址：", os.environ.get("AI_GATEWAY_URL", "http://127.0.0.1:8765/ai"))

# ---- 1. 择校：网关对 school_recommend 直接返回失败信封，不调模型 ----
print("\n[1] school_recommend（不调模型，期望 code=1）")
req = {"scene": "school_recommend", "subject": "math",
       "input": {"profile": "本科：某二本，目标 350"}, "user_context": {}}
env = call_gateway(req)
print("   code      :", env.get("code"))
print("   data      :", env.get("data"))
print("   raw       :", env.get("raw"))
print("   protocol  :", (env.get("protocol") or {}).get("version"))
print("   去向      :", storage_route(env))

# ---- 2. 请求字段错误：两处 student_answer 冲突，期望 code=2 ----
print("\n[2] politics_quiz 两处作答冲突（期望 code=2）")
req = {"scene": "qa", "subject": "politics",
       "input": {"question": "什么是实践", "student_answer": "A 答案"},
       "user_context": {"task": "politics_quiz", "student_answer": "B 答案"}}
env = call_gateway(req)
print("   code      :", env.get("code"))
print("   raw       :", env.get("raw"))

# ---- 3. 需要模型的请求：本机只有 0.5b，网关配的是 7b，看它怎么回 ----
print("\n[3] error_analysis（要调模型，看本机 Ollama 是否扛得住）")
req = {"scene": "error_analysis", "subject": "math",
       "input": {"question": "求 x^2 在 x=2 处的导数",
                 "student_answer": "4"},
       "user_context": {}}
env = call_gateway(req)
print("   code      :", env.get("code"))
print("   model     :", env.get("model"))
print("   elapsed_ms:", env.get("elapsed_ms"))
print("   raw       :", str(env.get("raw"))[:300])
data = env.get("data")
if isinstance(data, dict):
    print("   error_type:", data.get("error_type"),
          "conf=", data.get("error_confidence"))
print("   去向      :", storage_route(env))
