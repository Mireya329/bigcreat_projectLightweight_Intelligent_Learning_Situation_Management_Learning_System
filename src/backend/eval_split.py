# -*- coding: utf-8 -*-
"""切分效果评估：把 ocr_split 的输出和人工标注的 golden_splits.json 比对，
输出召回率 / 精确率 / F1，以及漏掉的题和多余的切分。

为什么要这个脚本：没有标准答案的优化都是盲调。
每改一版切分算法，跑一次就知道是变好还是变坏。

用法：python src/backend/eval_split.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# 默认评估 ocr_split；传模块名可评估别的版本：
#   python src/backend/eval_split.py               -> v1 纯正则
#   python src/backend/eval_split.py ocr_split_v2  -> v2 补题版
mod_name = sys.argv[1] if len(sys.argv) > 1 else "ocr_split"
split_questions = getattr(__import__(mod_name), "split_questions")
from ocr_split import SRC          # noqa: E402  路径常量始终从 v1 取

GOLDEN = SRC.parent / "golden_splits.json"


def evaluate():
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    golden = g["questions"]
    pred = split_questions(SRC.read_text(encoding="utf-8"))

    # ---- 匹配规则 ----
    # 一道"切出来的题"如果它的首行落在某道 golden 题的行范围内，就算命中该题。
    # 用首行而不是整段重叠，是因为切分漏题时往往把两道题粘成一段，
    # 首行能命中说明"至少找到了这道题的开头"。
    matched = {}          # golden 下标 -> 命中的 pred 下标
    for gi, gq in enumerate(golden):
        for pi, pq in enumerate(pred):
            if gq["start_line"] <= pq["start_line"] <= gq["end_line"]:
                matched[gi] = pi
                break

    n_gold, n_pred, n_hit = len(golden), len(pred), len(matched)
    recall = n_hit / n_gold if n_gold else 0
    precision = n_hit / n_pred if n_pred else 0
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall) else 0)

    print("=" * 58)
    print("错题切分评估报告")
    print("=" * 58)
    print(f"标准答案题数: {n_gold}    切出题数: {n_pred}    命中: {n_hit}")
    print(f"召回率 Recall   : {recall:6.1%}   漏切 {n_gold - n_hit} 道")
    print(f"精确率 Precision: {precision:6.1%}   误切 {n_pred - n_hit} 道")
    print(f"F1              : {f1:6.1%}")

    # ---- 漏掉的题 ----
    miss = [(i, golden[i]) for i in range(n_gold) if i not in matched]
    if miss:
        print(f"\n【漏切 {len(miss)} 道】")
        for i, q in miss:
            print(f"  {q['section']} 第{q['no']}题  "
                  f"行{q['start_line']}-{q['end_line']}  {q['stem'][:30]}")

    # ---- 误切的题（首行不在任何 golden 题范围内）----
    hit_pi = set(matched.values())
    extra = [(i, pred[i]) for i in range(n_pred) if i not in hit_pi]
    if extra:
        print(f"\n【误切 {len(extra)} 道】")
        for i, q in extra:
            print(f"  行{q['start_line']}-{q['end_line']}  {q['lines'][0][:35]}")

    print(f"\n匹配明细：{len(matched)}/{n_gold} 道标准题被找到")
    return recall, precision, f1


if __name__ == "__main__":
    evaluate()
