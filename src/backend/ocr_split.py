# -*- coding: utf-8 -*-
"""错题切分：把 OCR 全文按章节/题号切成一道道题（先切分，验证后再入库）"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]        # 本脚本在 src/backend/ 下，往上2层才是项目根
SRC = ROOT / "data" / "ocr_fast_test" / "all_text.txt"

SEC_RE = re.compile(r'^([一二三四五六七八九十]+)、(.+)$')   # 一、选择题
Q_RE   = re.compile(r'^(\d+)\s*[.．、](.+)$')               # 1. / 2. / 3、 / 4．


def split_questions(text):
    lines = [l.strip() for l in text.splitlines()]
    lines = [l for l in lines if l]      # 去掉空行

    result = []
    cur = None          # 当前正在攒的题
    section = ""        # 当前章节名

    for l in lines:
        ms = SEC_RE.match(l)
        mq = Q_RE.match(l)

        if ms:                            # 章节行：更新章节，不进题目内容
            section = ms.group(0)
            continue
        if mq:                            # 题号行：上一题收工，开新题
            if cur:
                result.append(cur)
            cur = {"section": section, "no": mq.group(1), "lines": [l]}
        elif cur:                         # 普通行：归入当前题
            cur["lines"].append(l)

    if cur:                               # 最后一题别漏掉
        result.append(cur)
    return result


def main():
    text = SRC.read_text(encoding="utf-8")
    qs = split_questions(text)
    print(f"共切出 {len(qs)} 道题\n")

    for i, q in enumerate(qs, 1):
        body = "\n".join(q["lines"])
        print(f"第{i}题  章节={q['section'] or '(未识别)'}  题号={q['no']}"
              f"  {len(q['lines'])}行/{len(body)}字符")
        print("    首行:", q["lines"][0][:45])

    # 统计每题的选项（选择题特征）
    print("\n各题的 (A)(B)(C)(D) 选项数：")
    for i, q in enumerate(qs, 1):
        n = sum(1 for l in q["lines"] if re.match(r'^\(?[A-D]\)', l))
        print(f"  第{i}题: {n} 个")


if __name__ == "__main__":
    main()
