#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""同一道题连跑 N 次，看分类结果稳不稳。

背景：真机联调时把同一批 7 道题跑了两轮，error_type 与 error_confidence
大量不一致（id=16 第一轮 calculation_error/0，第二轮 method_gap/1.0）。
分类型功能若要自动入库，结果必须可复现，所以单独量化一次。

用法：
    venv\\Scripts\\python.exe src/backend/ai_repeat_test.py            # 默认 3 题 × 3 次
    venv\\Scripts\\python.exe src/backend/ai_repeat_test.py --q 5 --r 4
"""
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))

import ai_interface as ai
from db import get_conn

# 固定用小样本：干净题 + OCR 残题各若干，跑得完才有意义
CASES = [
    ("干净-导数", "求函数 f(x)=x^2+2x+1 在 x=3 处的导数。", "6"),
    ("干净-积分", "计算定积分 ∫(0到1) x^2 dx。", "1"),
    ("残题-极限", "Ox100001\nf(x0+∆x,y0+∆y)−f(xbsy6)\n(A)\nlim\n(B", "（未识别）"),
    ("残题-偏导", "2. 设z = arctan\n(x-y)2+(x+y){2}\nx-y\n2×y334", "（未识别）"),
    ("残题-混合", "3. 设f(x,y)=ln|x+y|-e，求\n在点(1,0)处的值.\n∂x∂y\nx+y-1", "（未识别）"),
]


def run(nq: int, nr: int) -> list[dict]:
    print("=" * 62)
    print(f"可复现性测试：{nq} 题 × {nr} 次（模型 {ai.MODEL_NAME}）")
    print("=" * 62)

    out = []
    for name, q, ans in CASES[:nq]:
        print(f"\n--- {name} ---")
        types, confs, codes = [], [], []
        for i in range(nr):
            r = ai.analyze_error(question=q, student_answer=ans, subject="math")
            d = r.data or {}
            et = d.get("error_type") if r.code == 0 else f"降级(code={r.code})"
            cf = d.get("error_confidence") if r.code == 0 else None
            types.append(et)
            confs.append(cf)
            codes.append(r.code)
            print(f"  第{i+1}次: code={r.code} type={et} conf={cf} "
                  f"({r.elapsed_ms}ms, 重试{r.retries}次)")

        c = Counter(types)
        stable = len(c) == 1
        cf_set = sorted({x for x in confs if isinstance(x, (int, float))})
        print(f"  → 分类{'一致' if stable else f'不一致（{dict(c)}）'}"
              f"  置信度取值 {cf_set}")
        out.append({
            "name": name, "types": types, "confs": confs, "codes": codes,
            "stable": stable, "dist": dict(c),
        })
    return out


def summarize(rows: list[dict]) -> None:
    print("\n" + "=" * 62)
    print("汇总")
    print("=" * 62)
    stable = sum(1 for r in rows if r["stable"])
    print(f"分类完全一致: {stable}/{len(rows)} 题")
    for r in rows:
        mark = "✓" if r["stable"] else "✗"
        print(f"  {mark} {r['name']}: {r['dist']}")
    print()
    if stable == len(rows):
        print("结论：该模型在当前参数下分类可复现，可考虑自动入库")
    else:
        print("结论：同一输入多次调用分类不一致，自动入库会把随机结果写成事实，")
        print("      现阶段只能人工复核，或等 7b 后重测")


def main() -> int:
    nq, nr = 3, 3
    argv = sys.argv
    for flag, key in (("--q", "nq"), ("--r", "nr")):
        if flag in argv:
            try:
                v = int(argv[argv.index(flag) + 1])
                if key == "nq":
                    nq = v
                else:
                    nr = v
            except (IndexError, ValueError):
                pass

    if not ai.check_ollama_alive():
        print(f"Ollama 不在线（{ai.OLLAMA_BASE_URL}），先跑 ollama serve")
        return 1

    t0 = time.time()
    summarize(run(nq, nr))
    print(f"\n总耗时 {(time.time()-t0)/60:.1f} 分钟")
    return 0


if __name__ == "__main__":
    sys.exit(main())
