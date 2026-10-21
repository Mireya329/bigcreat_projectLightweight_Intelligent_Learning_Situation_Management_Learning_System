# -*- coding: utf-8 -*-
"""题库导入：目录扫描 → 命名解析 → 文本提取 → 切分 → 入 question_bank。

四号《题库资料对接说明》2026-10-21 定的三层目录与命名：

    真题\\科目\\批次\\文件
    考研：{科目简称}_{年份}_{真题|解析}.pdf      例 政治_2024_真题.pdf
    四六级：{级}_{年-月}_第N套_{真题|解析}.pdf   例 四级_2024-06_第1套_真题.pdf
           {级}_{年-月}_第N套_听力.mp3
    特殊：{级}_{年-月}_听力.mp3（不带套号，该场次所有套共用）

设计原则（沿用 ocr_to_db 的教训）：
  · **默认增量**：UNIQUE(subject_code, source_file, no_in_paper)，
    重跑只更新不新增，绝不 DELETE 整表
  · **不编造**：解析 PDF 是扫描件、提不出文本时，answer_text/analysis 留空
    并把 text_source 标成 'empty'，不用占位符假装成功
  · **不静默丢弃**：疑似噪声题（考生注意事项之类）单独计数并列出，
    由人决定，不在导入时悄悄扔掉

用法：
    python src/backend/question_bank_import.py --dry-run          # 只报告不写库
    python src/backend/question_bank_import.py                    # 真跑
    python src/backend/question_bank_import.py --root <目录>       # 换题库根目录
"""
import argparse
import json
import re
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("缺 pypdf：venv/Scripts/python.exe -m pip install pypdf")

import ocr_split_v2 as splitter                     # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BANK = ROOT / "data" / "question_bank_raw"

# 8 个资料目录 → schema 五科 code（四号文档第三节表，已核对）
SUBJECT_MAP = {
    "考研政治":   "POSTGRAD_POLITICS",
    "考研数学一": "POSTGRAD_MATH",
    "考研数学二": "POSTGRAD_MATH",
    "考研数学三": "POSTGRAD_MATH",
    "考研英语一": "POSTGRAD_ENGLISH",
    "考研英语二": "POSTGRAD_ENGLISH",
    "英语四级":   "CET4",
    "英语六级":   "CET6",
}

# 考研：政治_2024_真题.pdf / 数学二_2022_解析.pdf
RE_KAOYAN = re.compile(r"^(?P<subj>政治|数学[一二三]|英语[一二])_(?P<year>\d{4})_"
                       r"(?P<kind>真题|解析)\.pdf$")
# 四六级带套号：四级_2024-06_第1套_真题.pdf / 六级_2020-07_第1套_听力.mp3
RE_CET_SET = re.compile(r"^(?P<level>四级|六级)_(?P<session>\d{4}-\d{2})_"
                        r"第(?P<set>\d+)套_(?P<kind>真题|解析|听力)"
                        r"\.(?P<ext>pdf|mp3)$")
# 四六级共用听力：六级_2020-07_听力.mp3（不带套号）
RE_CET_SHARED = re.compile(r"^(?P<level>四级|六级)_(?P<session>\d{4}-\d{2})_"
                           r"(?P<kind>听力)\.(?P<ext>mp3)$")

# "考生注意事项"这类说明文字会被题号正则切成题，靠这些词识别出来
NOTICE_WORDS = ("答题前", "考生须", "条形码", "试题册", "注意事项", "答题卡")

# 选项行。⚠️ 四六级听力题的卷面是"题号 + 选项A"在同一行（题干在音频里），
# 例：'3. A) It plays a less important role...'
# 所以行首要允许题号前缀，否则 A) 会被当成题干的一部分（实测 266 题里
# 有一大批题干因此变成 "3. A) ..." 而 options 只剩 B/C/D）。
OPT_RE = re.compile(r"^\s*(?:[0-9]+[.、]\s*)?\(?\[?([A-D])[\)\]．、]\s*(.+)$")


def parse_name(name: str) -> dict:
    """从文件名解析出科目批次信息。返回含 err 表示不认识。"""
    m = RE_KAOYAN.match(name)
    if m:
        return {"kind": m["kind"], "year": int(m["year"]),
                "session": None, "paper_set": 1, "kaoyan": True}
    m = RE_CET_SET.match(name)
    if m:
        return {"kind": m["kind"], "year": int(m["session"][:4]),
                "session": m["session"], "paper_set": int(m["set"]),
                "kaoyan": False}
    m = RE_CET_SHARED.match(name)
    if m:
        return {"kind": "听力", "year": int(m["session"][:4]),
                "session": m["session"], "paper_set": None,   # 共用，不属于某一套
                "kaoyan": False}
    return {"err": "文件名不符合规范"}


def extract_text(path: Path) -> tuple:
    """返回 (全文, 字符数, 有字页数, 总页数)。

    ⚠️ 判定必须**逐页**看，不能只看总字符数（2026-10-21 踩过）：
    政治_2024_真题.pdf 封面是文字层、正文 12 页全是图，总共 373 字，
    按"总字符 ≥100"会被当成文字版，还切出 1 道根本不存在的"题"。
    所以按"有文字的页占比"判定，与 tools/probe_pdf_sample.py 同一口径。
    """
    try:
        r = PdfReader(str(path))
    except Exception as e:
        print(f"    ! 打不开 {path.name}：{type(e).__name__} {e}")
        return "", 0, 0, 0
    parts = []
    for p in r.pages:
        try:
            parts.append(p.extract_text() or "")
        except Exception:
            parts.append("")
    text = "\n".join(parts)
    with_text = sum(1 for t in parts if len(t.strip()) >= 100)
    return text, len(text.strip()), with_text, len(parts)


def classify(nchars: int, with_text: int, pages: int) -> str:
    """文字版 pdf_text / 扫描件 empty。"""
    if pages == 0:
        return "empty"
    return "pdf_text" if with_text / pages >= 0.8 else "empty"


def looks_like_notice(q: dict) -> bool:
    """题目是不是\"考生注意事项\"之类的说明文字（会被题号正则误切成题）。"""
    head = " ".join(q["lines"][:2])
    return any(w in head for w in NOTICE_WORDS)


def split_options(lines: list) -> tuple:
    """从题的若干行里分出题干与选项。返回 (题干, 选项列表)。"""
    opts = []
    stem_lines = []
    for ln in lines:
        s = ln.strip()
        m = OPT_RE.match(s)
        if m:
            # 从选项字母起截断，丢掉行首可能带有的题号（'3. A) x' -> 'A) x'）
            opts.append(s[m.start(1):].strip())
        else:
            stem_lines.append(ln)
    return "\n".join(stem_lines).strip(), opts


def guess_type(opts: list, stem: str) -> str:
    """题型。只判有把握的，判不准就返回空——不瞎猜。"""
    if opts:
        return "choice"
    return ""


def _find_subject_dirs(bank: Path):
    """找科目目录。

    真实题库是 `真题/考研政治/2024/`；但四号这次给的样本多包了一层
    `试导样本/`，直接扫会把"试导样本"当成未知科目。所以当前层找不到
    已知科目目录时，往下探一层再找（只探一层，不做无限递归）。
    """
    found = [p for p in sorted(bank.iterdir())
             if p.is_dir() and p.name in SUBJECT_MAP]
    if found:
        return found, bank
    for sub in sorted(p for p in bank.iterdir() if p.is_dir()):
        inner = [p for p in sorted(sub.iterdir())
                 if p.is_dir() and p.name in SUBJECT_MAP]
        if inner:
            print(f"  (根目录 {bank.name} 下未直接找到科目，"
                  f"下探一层到 {sub.name}/)")
            return inner, sub
    return [], bank


def iter_papers(bank: Path):
    """遍历 科目/批次/文件，产出 (科目code, 批次, 文件列表)。"""
    subj_dirs, base = _find_subject_dirs(bank)
    if not subj_dirs:
        print(f"  ! 在 {bank} 下没找到任何已知科目目录"
              f"（期望：{' / '.join(SUBJECT_MAP)}）")
        return
    for subj_dir in subj_dirs:
        code = SUBJECT_MAP[subj_dir.name]
        for batch in sorted(p for p in subj_dir.iterdir() if p.is_dir()):
            yield code, batch.name, sorted(batch.iterdir())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(DEFAULT_BANK), help="题库根目录")
    ap.add_argument("--dry-run", action="store_true", help="只报告不写库")
    ap.add_argument("--db", default=None, help="数据库路径（默认走 db.py）")
    args = ap.parse_args()

    bank = Path(args.root)
    if not bank.is_dir():
        sys.exit(f"题库目录不存在：{bank}")

    if args.db:
        conn = sqlite3.connect(args.db)
    else:
        sys.path.insert(0, str(HERE / "db"))
        from db import get_conn
        conn = get_conn()
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    stats = {"papers": 0, "pdf": 0, "scanned": 0, "questions": 0,
             "notice": 0, "inserted": 0, "updated": 0, "badname": 0,
             "no_stem": 0}
    notices = []
    scanned = []

    for code, batch, files in iter_papers(bank):
        print(f"\n[{code}] 批次 {batch}")
        # 同一套卷的真题/解析/听力归到一起
        for f in files:
            if f.suffix.lower() not in (".pdf", ".mp3"):
                continue
            info = parse_name(f.name)
            if "err" in info:
                stats["badname"] += 1
                print(f"    ? {f.name} —— {info['err']}")
                continue
            if f.suffix.lower() == ".mp3":
                continue                        # 听力音频只记路径，不单独成题
            if info["kind"] != "真题":
                continue                        # 解析 PDF 只做答案来源，题目来自真题

            stats["pdf"] += 1
            text, nchars, with_text, pages = extract_text(f)
            src = classify(nchars, with_text, pages)
            if src == "empty":
                stats["scanned"] += 1
                scanned.append(f"{code}/{batch}/{f.name}")
                print(f"    - {f.name}：{with_text}/{pages} 页有字"
                      f"（{nchars} 字）—— 扫描件，跳过切分")
                continue

            qs = splitter.split_questions(text)
            rel = str(f.relative_to(bank))
            # 找同名解析 PDF
            ana = f.parent / f.name.replace("_真题", "_解析")
            audio = f.parent / Path(f.name.replace("_真题", "_听力")).with_suffix(".mp3")
            got = 0
            for q in qs:
                if looks_like_notice(q):
                    stats["notice"] += 1
                    notices.append(f"{rel}#{q['no']}")
                    continue
                stem, opts = split_options(q["lines"])
                if not stem:
                    # 四六级听力题没有书面题干（题干在音频里），卷面只有选项。
                    # 这是资料本身的形态，不是切分失败——如实记下来报给人看，
                    # 不用占位文字假装题干存在。
                    stats["no_stem"] += 1
                rec = {
                    "subject_code": code,
                    "year": info["year"],
                    "session": info["session"],
                    "paper_set": info["paper_set"],
                    "question_type": guess_type(opts, stem),
                    "no_in_paper": str(q["no"]),
                    "section": q["section"] or None,
                    "question_text": stem,
                    "options": json.dumps(opts, ensure_ascii=False) if opts else None,
                    # answer / analysis：解析 PDF 全是扫描件，提不出来就留空，
                    # 不用占位符假装成功（宁可空着让人补）
                    "answer_text": None,
                    "analysis": None,
                    "source_file": rel,
                    "analysis_file": str(ana.relative_to(bank)) if ana.exists() else None,
                    "audio_file": str(audio.relative_to(bank)) if audio.exists() else None,
                    "text_source": src,
                    "text_chars": nchars,
                    "derived": 1 if q["derived"] else 0,
                    "status": "draft",
                }
                if args.dry_run:
                    got += 1
                    continue
                key = ("subject_code", "source_file", "no_in_paper")
                before = cur.execute(
                    "SELECT id FROM question_bank WHERE subject_code=? AND"
                    " source_file=? AND no_in_paper=?",
                    (rec["subject_code"], rec["source_file"], rec["no_in_paper"])
                ).fetchone()
                if before:
                    # 增量：只更新切分产物与来源信息，不动人工改过的字段
                    cur.execute(
                        "UPDATE question_bank SET year=:year, session=:session,"
                        " paper_set=:paper_set, question_type=:question_type,"
                        " section=:section, question_text=:question_text,"
                        " options=:options, analysis_file=:analysis_file,"
                        " audio_file=:audio_file, text_source=:text_source,"
                        " text_chars=:text_chars, derived=:derived WHERE id=:id",
                        {**rec, "id": before["id"]})
                    stats["updated"] += 1
                else:
                    cur.execute(
                        "INSERT INTO question_bank (subject_code, year, session,"
                        " paper_set, question_type, no_in_paper, section,"
                        " question_text, options, answer_text, analysis,"
                        " source_file, analysis_file, audio_file, text_source,"
                        " text_chars, derived, status) VALUES"
                        " (:subject_code,:year,:session,:paper_set,:question_type,"
                        " :no_in_paper,:section,:question_text,:options,"
                        " :answer_text,:analysis,:source_file,:analysis_file,"
                        " :audio_file,:text_source,:text_chars,:derived,:status)",
                        rec)
                    stats["inserted"] += 1
                got += 1
            stats["questions"] += got
            stats["papers"] += 1
            print(f"    + {f.name}：{nchars} 字 → {got} 题")

    if not args.dry_run:
        conn.commit()
    conn.close()

    print("\n" + "=" * 60)
    print(f"真题 PDF 处理：{stats['pdf']}")
    print(f"  其中扫描件（0 字符，未切分）：{stats['scanned']}")
    print(f"文字版切出题目：{stats['questions']}")
    print(f"  疑似噪声（考生注意事项类）已跳过：{stats['notice']}")
    print(f"  无书面题干（听力题，题干在音频里）：{stats['no_stem']}")
    print(f"命名不规范：{stats['badname']}")
    if not args.dry_run:
        print(f"入库：新增 {stats['inserted']}，更新 {stats['updated']}")
    else:
        print("（--dry-run，未写库）")
    if scanned:
        print("\n扫描件清单（需走 PaddleOCR）：")
        for s in scanned:
            print("  -", s)
    if notices:
        print(f"\n噪声题样本（前 5 条，共 {len(notices)}）：")
        for s in notices[:5]:
            print("  -", s)


if __name__ == "__main__":
    main()
