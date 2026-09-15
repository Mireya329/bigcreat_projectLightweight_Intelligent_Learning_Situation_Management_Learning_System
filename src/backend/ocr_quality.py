# -*- coding: utf-8 -*-
"""OCR 质量门禁：给每道入库前的题目打可信分，低分题不送 AI。

为什么需要它（A/B 实验的结论，见错误日志条目 18）：
给 Prompt 加"残缺就说残缺"的约束，9 题里只多救回 1 题，
残缺最严重的题模型照样编造 —— 说明**不能把可靠性寄托在模型自觉上**。
所以在入库前用规则给题目打分，低分题直接拦下，从源头切断"垃圾进幻觉出"。

设计原则：规则必须可解释 —— 每道题都能列出"为什么扣分"，
这样调阈值时有据可依，也方便向老师解释系统为什么拒答。

用法：
    python src/backend/ocr_quality.py              # 给库里所有题打分并打印
    python src/backend/ocr_quality.py --min 70     # 同时列出会被拦下的题
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn, DB_PATH          # noqa: E402

# 日文假名 / 韩文 / 带重音的拉丁字母 —— OCR 认错字时最常吐出来的东西
SUSPICIOUS = re.compile(r'[\u3040-\u30ff\uac00-\ud7afÀ-ÿĀ-ſ]')
CJK = re.compile(r'[\u4e00-\u9fff]')
# 字母数字混杂的长串（如 Ox100001、xbsy6）—— 典型乱码
GARBLED = re.compile(r'[A-Za-z0-9]{5,}')
# 行首题号
QNO_HEAD = re.compile(r'^\s*(\d+\s*[.．、]|Ox\d)')


def score_text(text: str):
    """给一段题目文本打分（0-100），返回 (分数, 扣分理由列表)"""
    reasons = []
    if not text or not text.strip():
        return 0, ["空文本"]

    score = 100
    n = len(text)

    # 1. 可疑字符占比
    sus = len(SUSPICIOUS.findall(text))
    ratio = sus / n
    if ratio > 0.05:
        score -= 30
        reasons.append(f"可疑字符占比 {ratio:.1%}（-30）")
    elif ratio > 0.02:
        score -= 15
        reasons.append(f"可疑字符占比 {ratio:.1%}（-15）")

    # 2. 中文占比过低：试卷题干通常有中文说明，纯符号说明切到的是公式碎片
    cjk_ratio = len(CJK.findall(text)) / n
    if cjk_ratio < 0.10:
        score -= 20
        reasons.append(f"中文占比仅 {cjk_ratio:.1%}（-20）")

    # 3. 行首没有题号（题号被 OCR 丢失或认错）
    if not QNO_HEAD.match(text.strip()):
        score -= 15
        reasons.append("题号缺失或被误识别（-15）")

    # 4. 疑似乱码串：字母数字混杂且无空格
    garb = [w for w in GARBLED.findall(text)
            if re.search(r"\d", w) and re.search(r"[A-Za-z]", w)]
    if garb:
        d = min(30, 10 * len(garb))
        score -= d
        reasons.append(f"疑似乱码串 {garb[:2]}（-{d}）")

    # 5. 文本过短，多半是被切碎的残片
    if n < 30:
        score -= 20
        reasons.append(f"文本过短 {n} 字符（-20）")

    if not reasons:
        reasons.append("未发现明显质量问题")
    return max(0, score), reasons


def ensure_column(conn):
    """老库没有 ocr_quality 字段时自动加（幂等）"""
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(error_items)")]
    if "ocr_quality" not in cols:
        conn.execute("ALTER TABLE error_items ADD COLUMN ocr_quality INTEGER")
        conn.commit()
        print("[迁移] 已给 error_items 增加 ocr_quality 字段")
    return "ocr_quality" in [
        r["name"] for r in conn.execute("PRAGMA table_info(error_items)")]


def main():
    argv = sys.argv
    min_score = 70
    if "--min" in argv:
        min_score = int(argv[argv.index("--min") + 1])

    conn = get_conn()
    ensure_column(conn)

    rows = conn.execute(
        "SELECT id, question_text, source FROM error_items ORDER BY id").fetchall()
    print(f"=== OCR 质量评分（阈值 {min_score}）===\n")

    blocked = []
    for r in rows:
        s, reasons = score_text(r["question_text"])
        conn.execute("UPDATE error_items SET ocr_quality=? WHERE id=?", (s, r["id"]))
        tag = "✅ 放行" if s >= min_score else "🚫 拦下"
        src = r["source"].split("｜")[-1]
        print(f"id={r['id']:<3} {s:>3}分 {tag}  {src}")
        for x in reasons:
            print(f"          - {x}")
        if s < min_score:
            blocked.append(r["id"])

    conn.commit()
    print(f"\n共 {len(rows)} 题，拦下 {len(blocked)} 题：{blocked}")
    print("（拦下的题不会送 AI，直接标记需人工确认）")
    print("数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
