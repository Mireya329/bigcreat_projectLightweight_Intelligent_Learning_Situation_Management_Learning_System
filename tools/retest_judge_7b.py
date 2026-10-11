#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""judge_fragment 在 7B 上的可复现性重测。

背景：
  0.5b 时代实测该场景完全随机（3 题 × 3 次，9 次判定无一一致），
  判错会 INSERT 假题，所以当时加了"稳定性校验失败强制判 C"的兜底，
  等于这个功能实际上没启用。2026-10-11 本机装好 7b，重测一次，
  判断这个兜底能不能撤、漏题兜底能不能真正用起来。

用法：
    venv\\Scripts\\python.exe tools/retest_judge_7b.py              # 默认 5 例 × 3 次
    venv\\Scripts\\python.exe tools/retest_judge_7b.py --n 4 --r 3 --model qwen2.5:0.5b

只读正式库，不写任何数据。
"""
import argparse
import os
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src" / "backend"))

import ai_interface as ai                                    # noqa: E402

DB = ROOT / "data" / "learning.db"


def build_cases(n: int) -> list[tuple[str, str, str, str]]:
    """用库里真实的连续三行构造 (名称, prev, fragment, next)。

    中间那行当待判断片段——真实 OCR 切分里，它就是最容易漏掉的那类文本。
    优先挑 ocr_quality 低的，那才是兜底真正要处理的样本。
    """
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "SELECT id, question_text, ocr_quality FROM error_items"
        " WHERE question_text IS NOT NULL AND TRIM(question_text) != ''"
        " ORDER BY COALESCE(ocr_quality, 999) ASC, id ASC").fetchall()
    con.close()

    texts = [(r["id"], (r["question_text"] or "").strip()) for r in rows]
    cases = []
    for i in range(1, len(texts) - 1):
        if len(cases) >= n:
            break
        prev = texts[i - 1][1]
        frag = texts[i][1]
        nxt = texts[i + 1][1]
        # 三段都太短说明数据本身没信息，跳过
        if len(frag) < 4 or len(prev) < 3 or len(nxt) < 3:
            continue
        cases.append((f"id={texts[i][0]}", prev, frag, nxt))
    return cases


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=5, help="样本数")
    ap.add_argument("--r", type=int, default=3, help="每题跑几次")
    ap.add_argument("--model", default=ai.MODEL_NAME_7B, help="模型名")
    args = ap.parse_args()

    ai.AI_PROVIDER = "local"          # judge_fragment 网关不支持，只能直连
    ai.MODEL_NAME = args.model
    ai.STABILITY_CHECK = False        # 要测的是模型本身的稳定性，不能开兜底
    ai.MOCK_MODE = False

    if not ai.check_ollama_alive():
        print("模型服务不在线，先开 Ollama。")
        return 1

    cases = build_cases(args.n)
    if not cases:
        print("库里构不出样本（题目文本不足）。")
        return 1

    print("=" * 72)
    print(f"judge_fragment 可复现性重测   模型 {args.model}")
    print(f"样本 {len(cases)} 例 × {args.r} 次   （只读正式库，不写入）")
    print("=" * 72)

    stable = 0
    dist: Counter = Counter()
    t_all = time.time()

    for name, prev, frag, nxt in cases:
        picks = []
        for _ in range(args.r):
            resp = ai.judge_fragment(frag, prev, nxt)
            if resp.code == 0 and isinstance(resp.data, dict):
                picks.append(str(resp.data.get("choice", "?")).strip().upper()[:1])
            else:
                picks.append(f"失败(code={resp.code})")
        cnt = Counter(picks)
        top, topn = cnt.most_common(1)[0]
        ok = (topn == args.r)
        stable += 1 if ok else 0
        dist.update([top])
        tag = "一致" if ok else "不一致"
        print(f"\n{name}  片段：{frag[:34]!r}")
        print(f"   {args.r} 次判定：{list(picks)}   → {tag}")
        if not ok:
            # 只在真跑成功的情况下看模型给的理由
            resp = ai.judge_fragment(frag, prev, nxt)
            if resp.code == 0 and resp.data:
                print(f"   末次理由：{str(resp.data.get('reason', ''))[:70]}")

    mins = (time.time() - t_all) / 60
    print("\n" + "=" * 72)
    print(f"完全一致 {stable}/{len(cases)}   总耗时 {mins:.1f} 分钟")
    print(f"多数派分布：{dict(dist)}")
    if stable == len(cases):
        print("→ 7B 在该场景下可复现，兜底（强制判 C）具备撤除条件。")
    else:
        print("→ 7B 在该场景下仍不可复现，兜底必须保留。")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
