# -*- coding: utf-8 -*-
"""切分 v2：在 v1（纯正则题号）基础上做两处补题。

v1 的 58% 召回率暴露了两个问题：
  1. 章节之后、第一个题号之前的内容被整段丢弃（选择题第 1、2 题就是这样没的）
  2. 没有题号行时，相邻两题会被粘成一坨（选择题第 3、4 题粘在一起）

v2 的补法：
  1. 孤儿块回收：章节内、首个题号之前的行，单独收成一段"疑似题"
  2. 选项块二次切分：一道题里如果出现多组 (A)，说明粘了多道选择题，按 (A) 切开

用法：python src/backend/ocr_split_v2.py
评估：python src/backend/eval_split.py ocr_split_v2
"""
import re

from ocr_split import SEC_RE, Q_RE, SRC          # 复用 v1 的正则和路径

A_OPT_RE = re.compile(r'^\(?A\)')                # 只认 (A)，作为一组的开始


def _mk(section, no, items, derived=False):
    """把 [(行号, 文本)] 组装成一道题"""
    q = {
        "section": section,
        "no": no,
        "lines": [t for _, t in items],
        "line_nos": [i for i, _ in items],
        "derived": derived,
    }
    q["start_line"] = q["line_nos"][0]
    q["end_line"] = q["line_nos"][-1]
    return q


def _split_by_option(q):
    """一道题里如果有多个 (A)，按 (A) 的位置切成多道"""
    a_idx = [i for i, l in enumerate(q["lines"]) if A_OPT_RE.match(l)]
    if len(a_idx) <= 1:
        return [q]                                # 只有一组选项，不用切

    out = []
    bounds = a_idx + [len(q["lines"])]
    for k in range(len(a_idx)):
        start = 0 if k == 0 else bounds[k]        # 第一组保留题干，后续组从 (A) 开始
        end = bounds[k + 1]
        if start >= end:
            continue
        seg = list(zip(q["line_nos"][start:end], q["lines"][start:end]))
        out.append(_mk(q["section"],
                       q["no"] if k == 0 else f"{q['no']}+{k}",
                       seg, derived=(k > 0)))
    return out


def split_questions(text):
    items = [(i, l.strip()) for i, l in enumerate(text.splitlines(), 1)]
    items = [(i, s) for i, s in items if s]

    result = []
    cur = None
    section = ""
    orphan = []          # 章节之后、首个题号之前的孤儿行

    for i, l in items:
        ms = SEC_RE.match(l)
        mq = Q_RE.match(l)

        if ms:                                    # 换章节：先收掉上一段孤儿
            if orphan and section:
                result.append(_mk(section, "0", orphan, derived=True))
            orphan = []
            section = ms.group(0)
            continue

        if mq:                                    # 遇到题号：孤儿先成题，再开新题
            if orphan and section:
                result.append(_mk(section, "0", orphan, derived=True))
            orphan = []
            if cur:
                result.append(cur)
            cur = _mk(section, mq.group(1), [(i, l)])
        else:
            if cur:
                cur["lines"].append(l)
                cur["line_nos"].append(i)
                cur["end_line"] = i
            elif section:                         # 还没开题 → 孤儿行（页眉等无章节的行会被丢弃）
                orphan.append((i, l))

    if orphan and section:
        result.append(_mk(section, "0", orphan, derived=True))
    if cur:
        result.append(cur)

    # 对每段做选项块二次切分
    out = []
    for q in result:
        out.extend(_split_by_option(q))
    return out


def main():
    qs = split_questions(SRC.read_text(encoding="utf-8"))
    print(f"共切出 {len(qs)} 道题（v2 补题版）\n")
    for i, q in enumerate(qs, 1):
        tag = " [补]" if q.get("derived") else ""
        print(f"第{i}题  章节={q['section'] or '(未识别)'}  题号={q['no']}"
              f"  行{q['start_line']}-{q['end_line']}{tag}")
        print("    首行:", q["lines"][0][:45])


if __name__ == "__main__":
    main()
