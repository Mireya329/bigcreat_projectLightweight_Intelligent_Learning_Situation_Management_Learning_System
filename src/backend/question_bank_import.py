# -*- coding: utf-8 -*-
"""题库导入 v2：目录扫描 → 命名解析 → 逐页提取 → 切分 → 入 question_bank（v2 结构）。

四号《题库资料对接说明》2026-10-21 的三层目录与命名：

    真题\\科目\\批次\\文件
    考研：{科目简称}_{年份}_{真题|解析}.pdf      例 政治_2024_真题.pdf
    四六级：{级}_{年-月}_第N套_{真题|解析}.pdf   例 四级_2024-06_第1套_真题.pdf
           {级}_{年-月}_第N套_听力.mp3
    特殊：{级}_{年-月}_听力.mp3（不带套号，该场次所有套共用）

按队长《给二号_Q01小批量试导交接要求》2026-10-21 落实的六条硬约束：
  1. **单一配置**：--db > LEARNING_DB_PATH > 默认正式库，写库前打印绝对路径；
  2. **不覆盖已人工核对**：status 不是 draft 的题只记冲突、不改内容；
  3. **错误可定位**：每条错误带 文件 / 题号 / 字段 / 原因，--report 导出 JSON；
  4. **事务策略明示**：默认整批提交（出错整批回滚），--commit-each 改逐条提交；
  5. **三种去重分工**：文件 SHA256 查重、来源编号（source_question_id）查重、
     题目内容查重各自独立，不互相替代；
  6. **资源路径相对化**：只存相对题库根目录的路径，换机器仍可定位；
     音频做可播放校验，缺音频**登记待确认**，绝不复制第一套的音频充数。

设计原则（沿用 ocr_to_db 的教训）：
  · **默认增量**：重跑只更新草稿态的题，绝不 DELETE 整表
  · **不编造**：解析 PDF 是扫描件、提不出文本时，answer/explanation 留空
    并把 text_source 标成 'empty'，不用占位符假装成功
  · **不静默丢弃**：疑似噪声题单独计数并列出，由人决定

用法：
    # 只检查不写库（正式跑之前必做）
    python src/backend/question_bank_import.py --dry-run --report /tmp/qb.json
    # 独立测试库试导
    python src/backend/question_bank_import.py --db data/learning_test_qbank.db
    # 逐条提交（出问题时只丢当前条）
    python src/backend/question_bank_import.py --commit-each
"""
import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "db"))

try:
    from pypdf import PdfReader
except ImportError:
    sys.exit("缺 pypdf：venv/Scripts/python.exe -m pip install pypdf")

import ocr_split_v2 as splitter                     # noqa: E402
from db import set_cli_db, announce_db, get_conn     # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BANK = ROOT / "data" / "question_bank_raw"

# 8 个资料目录 → (五大类 code, 卷型)。⚠️ 数学一二三、英语一二共用大类 code，
# 靠 paper_variant 区分，不能合并成同一份卷（队长 2026-10-21 第二节明确要求）。
SUBJECT_MAP = {
    "考研政治":   ("POSTGRAD_POLITICS", None),
    "考研数学一": ("POSTGRAD_MATH",     "MATH_1"),
    "考研数学二": ("POSTGRAD_MATH",     "MATH_2"),
    "考研数学三": ("POSTGRAD_MATH",     "MATH_3"),
    "考研英语一": ("POSTGRAD_ENGLISH",  "ENGLISH_1"),
    "考研英语二": ("POSTGRAD_ENGLISH",  "ENGLISH_2"),
    "英语四级":   ("CET4",              None),
    "英语六级":   ("CET6",              None),
}

RE_KAOYAN = re.compile(r"^(?P<subj>政治|数学[一二三]|英语[一二])_(?P<year>\d{4})_"
                       r"(?P<kind>真题|解析)\.pdf$")
RE_CET_SET = re.compile(r"^(?P<level>四级|六级)_(?P<session>\d{4}-\d{2})_"
                        r"第(?P<set>\d+)套_(?P<kind>真题|解析|听力)"
                        r"\.(?P<ext>pdf|mp3)$")
RE_CET_SHARED = re.compile(r"^(?P<level>四级|六级)_(?P<session>\d{4}-\d{2})_"
                           r"(?P<kind>听力)\.(?P<ext>mp3)$")

# "考生注意事项"这类说明文字会被题号正则切成题（它们自己就带 1. 2. 3. 编号）。
# ⚠️ 词表是实测补出来的：2026-10-21 英语一_2024 的第 4 条注意事项
# "填(书)写部分必须使用黑色字迹签字笔书写"不含原来的任何关键词，被当成第 4 题，
# 与真正的第 4 题撞了 source_question_id——这才暴露出来。
NOTICE_WORDS = ("答题前", "考生须", "条形码", "试题册", "注意事项", "答题卡",
                "填涂", "签字笔", "黑色字迹", "密封", "考场", "开考")

# 选项行。⚠️ 四六级听力题的卷面是"题号 + 选项A"在同一行（题干在音频里），
# 例：'3. A) It plays a less important role...'
# 所以行首要允许题号前缀，否则 A) 会被当成题干的一部分。
OPT_RE = re.compile(r"^\s*(?:[0-9]+[.、]\s*)?\(?\[?([A-D])[\)\]．、]\s*(.+)$")

# 非草稿态 = 有人看过 / 已核对过，导入不许改内容，只能进冲突清单
LOCKED_STATUS = ("pending_review", "verified")

# 四六级卷面固定结构：Part I 写作（无题号）、Part II 听力（1-25）、
# Part III 阅读（26-45）、Part IV 翻译（46）。音频只挂到听力题号上，
# 不能因为"这套卷有音频"就给全卷 46 题都挂上（2026-10-21 实测会误挂 184 题）。
CET_LISTENING_RANGE = (1, 25)


def qno_int(no: str):
    """题号取整数部分；'25+1' -> 25，'(1)' 之类取不到返回 None。"""
    m = re.match(r"\s*(\d+)", str(no))
    return int(m.group(1)) if m else None


def is_listening_question(info: dict, no: str) -> bool:
    """是不是听力题。考研卷没有听力，直接 False。"""
    if info.get("kaoyan"):
        return False
    n = qno_int(no)
    return n is not None and CET_LISTENING_RANGE[0] <= n <= CET_LISTENING_RANGE[1]


def parse_name(name: str) -> dict:
    """从文件名解析科目批次信息。返回含 err 表示不认识。"""
    m = RE_KAOYAN.match(name)
    if m:
        return {"kind": m["kind"], "year": int(m["year"]),
                "session": None, "paper_set": 1, "kaoyan": True}
    m = RE_CET_SET.match(name)
    if m:
        return {"kind": m["kind"], "year": int(m["session"][:4]),
                "session": m["session"], "paper_set": int(m["set"]),
                "level": m["level"], "kaoyan": False}
    m = RE_CET_SHARED.match(name)
    if m:
        return {"kind": "听力", "year": int(m["session"][:4]),
                "session": m["session"], "paper_set": None,
                "level": m["level"], "kaoyan": False}
    return {"err": "文件名不符合规范"}


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(chunk), b""):
            h.update(blk)
    return h.hexdigest()


def probe_audio(path: Path) -> tuple:
    """音频能不能播：只看文件头判断容器，**不解码、不确认内容对不对**。

    返回 (容器类型, 是否可播, 说明)。
    ⚠️ 实测（2026-10-21）：样本里的 4 个听力文件，3 个是 MP3（ID3 头），
    六级_2024-06_第1套_听力.mp3 的文件头是 `ftypM4A ` ,**是 M4A 容器被改名成
    .mp3**。浏览器普遍能播 M4A，所以判定为可播，但要单独提示格式不一致，
    不能假装它是 MP3。
    """
    try:
        head = path.open("rb").read(12)
    except OSError as e:
        return "unknown", False, f"无法读取：{e}"
    if len(head) < 4:
        return "unknown", False, "文件过小，不是有效音频"
    if head[:3] == b"ID3":
        return "mp3", True, "MP3（ID3 标签头）"
    if head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        return "mp3", True, "MP3（MPEG 帧同步头）"
    if head[4:8] == b"ftyp":
        return "m4a", True, f"MP4/M4A 容器（{head[8:12].decode('ascii', 'replace')}）"
    return "unknown", False, f"文件头 {head[:8].hex()} 不是已知音频容器"


def extract_pages(path: Path) -> tuple:
    """返回 (全文, 逐页文本列表, 有字页数, 总页数)。

    ⚠️ 判定必须**逐页**看，不能只看总字符数（2026-10-21 踩过）：
    政治_2024_真题.pdf 封面是文字层、正文 12 页全是图，总共 373 字，
    按"总字符 ≥100"会被当成文字版，还切出 1 道根本不存在的"题"。
    """
    try:
        r = PdfReader(str(path))
    except Exception as e:
        print(f"    ! 打不开 {path.name}：{type(e).__name__} {e}")
        return "", [], 0, 0
    parts = []
    for p in r.pages:
        try:
            parts.append(p.extract_text() or "")
        except Exception:
            parts.append("")
    with_text = sum(1 for t in parts if len(t.strip()) >= 100)
    return "\n".join(parts), parts, with_text, len(parts)


def classify(pages: list) -> str:
    """文字版 pdf_text / 扫描件 empty。按有字页占比判定。"""
    if not pages:
        return "empty"
    with_text = sum(1 for t in pages if len(t.strip()) >= 100)
    return "pdf_text" if with_text / len(pages) >= 0.8 else "empty"


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def locate_pages(pages: list, q: dict) -> list:
    """题目落在 PDF 的第几页（从 1 开始的页序）。

    做法：拿题干前几个非空行去逐页文本里找；找不到就返回空列表——
    **宁可空着也不猜**（页眉页脚、换行都可能导致匹配不上）。
    """
    norm_pages = [_norm(p) for p in pages]
    cands = [ln.strip() for ln in q["lines"] if ln.strip()]
    for cand in cands[:3]:
        if len(cand) < 8:
            continue
        key = _norm(cand)[:30]
        if len(key) < 8:
            continue
        hit = [i for i, p in enumerate(norm_pages, 1) if key in p]
        if hit:
            return sorted(set(hit))
    return []


def looks_like_notice(q: dict) -> bool:
    """看前 3 行：说明性文字通常就一两句，真题目不会在开头写这些词。"""
    head = " ".join(q["lines"][:3])
    return any(w in head for w in NOTICE_WORDS)


def split_options(lines: list) -> tuple:
    """从题的若干行里分出题干与选项。返回 (题干, 选项列表)。"""
    opts, stem_lines = [], []
    for ln in lines:
        s = ln.strip()
        m = OPT_RE.match(s)
        if m:
            opts.append(s[m.start(1):].strip())
        else:
            stem_lines.append(ln)
    return "\n".join(stem_lines).strip(), opts


def guess_type(opts: list) -> str:
    """题型。只判有把握的：有选项就是单选候选；多选/主观题留给人工改。

    ⚠️ 政治多选题（17、18 题这类）机器分不出来，判成 single_choice 后
    必须人工改成 multi_choice，否则判分会错。
    """
    return "single_choice" if opts else ""


def _find_subject_dirs(bank: Path):
    found = [p for p in sorted(bank.iterdir())
             if p.is_dir() and p.name in SUBJECT_MAP]
    if found:
        return found
    for sub in sorted(p for p in bank.iterdir() if p.is_dir()):
        inner = [p for p in sorted(sub.iterdir())
                 if p.is_dir() and p.name in SUBJECT_MAP]
        if inner:
            print(f"  (根目录 {bank.name} 下未直接找到科目，下探一层到 {sub.name}/)")
            return inner
    return []


def iter_papers(bank: Path):
    subj_dirs = _find_subject_dirs(bank)
    if not subj_dirs:
        print(f"  ! 在 {bank} 下没找到任何已知科目目录"
              f"（期望：{' / '.join(SUBJECT_MAP)}）")
        return
    for subj_dir in subj_dirs:
        code, variant = SUBJECT_MAP[subj_dir.name]
        for batch in sorted(p for p in subj_dir.iterdir() if p.is_dir()):
            yield code, variant, batch.name, sorted(batch.iterdir())


def resolve_audio(f: Path, info: dict, bank: Path, errors: list, rel: str):
    """找这套卷的听力音频。返回 (相对路径列表, material_id)。

    顺序：本套专用 → 该场次共用 → 都没有就登记待确认。
    ⚠️ 第3套没带音频是资料本身如此（PDF 里写明"听力与前两套相同、选项顺序
    不同"），**不能拿第1套的音频顶上**——选项顺序不同，答案也不同。
    """
    if info.get("kaoyan"):
        return [], None
    level, session, pset = info.get("level"), info.get("session"), info.get("paper_set")
    if not level:
        return [], None
    own = f.parent / f"{level}_{session}_第{pset}套_听力.mp3"
    shared = f.parent / f"{level}_{session}_听力.mp3"
    for cand, tag in ((own, f"{level}_{session}_第{pset}套"),
                      (shared, f"{level}_{session}_共用")):
        if not cand.exists():
            continue
        kind, ok, why = probe_audio(cand)
        relp = str(cand.relative_to(bank))
        if not ok:
            errors.append({"file": rel, "question_no": "-", "field": "audio_refs_json",
                           "level": "error",
                           "reason": f"音频不可播放（{why}）：{relp}"})
            return [], None
        if kind == "m4a":
            # 可播，但格式与其余文件不一致，登记提示而不是默默接受
            errors.append({"file": rel, "question_no": "-", "field": "audio_refs_json",
                           "level": "warn",
                           "reason": f"{relp} 实为 {why}，扩展名是 .mp3 但内容不是 MP3"
                                     f"——请四号确认是否统一转码"})
        return [relp], f"{level}|{session}|listening|{tag}"
    return [], None      # 缺音频由调用方按"本卷有没有听力题"来判断该怎么报


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(DEFAULT_BANK), help="题库根目录")
    ap.add_argument("--db", default=None, help="数据库路径（覆盖环境变量）")
    ap.add_argument("--dry-run", action="store_true", help="只检查不写库")
    ap.add_argument("--commit-each", action="store_true",
                    help="逐条提交（默认整批提交，出错整批回滚）")
    ap.add_argument("--report", default=None, help="导出 JSON 报告路径")
    args = ap.parse_args()

    bank = Path(args.root).resolve()
    if not bank.is_dir():
        sys.exit(f"题库目录不存在：{bank}")
    print(f"题库根目录：{bank}")

    set_cli_db(args.db)
    db_path = announce_db(role="题库导入")

    conn = get_conn()
    cur = conn.cursor()
    # 表不存在就明说，别让 SQL 报错把人带到沟里
    if not cur.execute("SELECT name FROM sqlite_master WHERE type='table'"
                       " AND name='question_bank'").fetchone():
        sys.exit("当前库没有 question_bank 表，先跑 "
                 "python src/backend/db/migrate_question_bank_v2.py")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    stats = {"papers": 0, "pdf": 0, "scanned": 0, "questions": 0,
             "inserted": 0, "updated": 0, "skipped_conflict": 0, "failed": 0,
             "notice": 0, "no_stem": 0, "badname": 0, "missing_audio": 0,
             "dup_file": 0}
    errors, conflicts, scanned, notices = [], [], [], []
    seen_sha = {}        # 文件指纹 -> 首个文件（同内容不同名的重复文件）
    seen_ids = set()     # 本批次内 source_question_id 查重

    try:
        for code, variant, batch, files in iter_papers(bank):
            print(f"\n[{code}{'/' + variant if variant else ''}] 批次 {batch}")
            for f in files:
                if f.suffix.lower() not in (".pdf", ".mp3"):
                    continue
                info = parse_name(f.name)
                if "err" in info:
                    stats["badname"] += 1
                    errors.append({"file": f.name, "question_no": "-",
                                   "field": "文件名", "level": "error",
                                   "reason": info["err"]})
                    print(f"    ? {f.name} —— {info['err']}")
                    continue
                if f.suffix.lower() == ".mp3":
                    continue                     # 音频只登记引用，不单独成题
                if info["kind"] != "真题":
                    continue                     # 解析 PDF 只做答案来源

                stats["pdf"] += 1
                rel = str(f.relative_to(bank))
                digest = sha256_of(f)
                if digest in seen_sha:
                    stats["dup_file"] += 1
                    errors.append({"file": rel, "question_no": "-",
                                   "field": "file_sha256", "level": "warn",
                                   "reason": f"与 {seen_sha[digest]} 内容完全相同（重复文件）"})
                    print(f"    = {f.name}：与 {seen_sha[digest]} 同内容，跳过")
                    continue
                seen_sha[digest] = rel

                text, pages, with_text, npages = extract_pages(f)
                src = classify(pages)
                if src == "empty":
                    stats["scanned"] += 1
                    scanned.append(f"{rel}（{with_text}/{npages} 页有字）")
                    print(f"    - {f.name}：{with_text}/{npages} 页有字"
                          f" —— 扫描件，跳过切分")
                    continue

                audio_refs, material_id = resolve_audio(f, info, bank, errors, rel)
                ana = f.parent / f.name.replace("_真题", "_解析")
                qs = splitter.split_questions(text)
                got = 0
                n_listening = 0
                for q in qs:
                    if looks_like_notice(q):
                        stats["notice"] += 1
                        notices.append(f"{rel}#{q['no']}")
                        continue
                    stem, opts = split_options(q["lines"])
                    hit_pages = locate_pages(pages, q)
                    listening = is_listening_question(info, q["no"])
                    if listening:
                        n_listening += 1
                    if not stem:
                        # 四六级听力题没有书面题干（题干在音频里），卷面只有选项。
                        # 这是资料本身的形态，不是切分失败——如实记录，不用占位文字。
                        stats["no_stem"] += 1
                    qid = "|".join([code, variant or "-",
                                    info["session"] or str(info["year"] or "-"),
                                    str(info["paper_set"] if info["paper_set"] is not None
                                        else "-"),
                                    str(q["no"])])
                    if qid in seen_ids:
                        conflicts.append({"source_question_id": qid, "source_file": rel,
                                          "question_no": q["no"], "field": "source_question_id", "level": "error",
                                          "reason": "本批次内来源编号重复"})
                        continue
                    seen_ids.add(qid)

                    rec = {
                        "source_question_id": qid,
                        "subject_code": code,
                        "paper_variant": variant,
                        "year": info["year"],
                        "session": info["session"],
                        "paper_set": info["paper_set"],
                        "question_no": str(q["no"]),
                        "question_type": guess_type(opts),
                        "knowledge_points_json": None,
                        "stem": stem,
                        "options_json": json.dumps(opts, ensure_ascii=False) if opts else None,
                        "answer": None,          # 解析 PDF 是扫描件，提不出来就留空
                        "explanation": None,
                        "source_file": rel,
                        "source_pages_json": json.dumps(hit_pages) if hit_pages else None,
                        "image_refs_json": None,
                        "audio_refs_json": json.dumps(audio_refs, ensure_ascii=False)
                                            if (listening and audio_refs) else None,
                        "material_id": material_id if listening else None,
                        "status": "draft",
                        "section": q["section"] or None,
                        "analysis_file": str(ana.relative_to(bank)) if ana.exists() else None,
                        "print_page": None,
                        "file_sha256": digest,
                        "text_source": src,
                        "text_chars": len(text.strip()),
                        "derived": 1 if q["derived"] else 0,
                    }
                    if args.dry_run:
                        got += 1
                        continue

                    old = cur.execute(
                        "SELECT id, status, stem, answer, explanation FROM question_bank"
                        " WHERE source_question_id=?", (qid,)).fetchone()
                    try:
                        if old:
                            if old["status"] in LOCKED_STATUS:
                                # 队长第四节：不能悄悄覆盖已人工核对的内容
                                stats["skipped_conflict"] += 1
                                conflicts.append({
                                    "source_question_id": qid, "source_file": rel,
                                    "question_no": q["no"], "field": "stem/answer/explanation",
                                    "reason": f"库中 status={old['status']}，非草稿态，"
                                              f"导入不覆盖，需人工裁决",
                                    "existing_value": (old["stem"] or "")[:80],
                                    "incoming_value": stem[:80]})
                                continue
                            cur.execute(
                                "UPDATE question_bank SET paper_variant=:paper_variant,"
                                " year=:year, session=:session, paper_set=:paper_set,"
                                " question_type=:question_type, section=:section,"
                                " stem=:stem, options_json=:options_json,"
                                " source_pages_json=:source_pages_json,"
                                " audio_refs_json=:audio_refs_json,"
                                " material_id=:material_id, analysis_file=:analysis_file,"
                                " file_sha256=:file_sha256, text_source=:text_source,"
                                " text_chars=:text_chars, derived=:derived,"
                                " updated_at=datetime('now','localtime')"
                                " WHERE id=:id",
                                {**rec, "id": old["id"]})
                            stats["updated"] += 1
                        else:
                            keys = ",".join(rec.keys())
                            marks = ",".join(":" + k for k in rec.keys())
                            cur.execute(
                                f"INSERT INTO question_bank ({keys}) VALUES ({marks})", rec)
                            stats["inserted"] += 1
                    except sqlite3.Error as e:
                        stats["failed"] += 1
                        errors.append({"file": rel, "question_no": q["no"],
                                       "field": "DB", "level": "error",
                                       "reason": f"{type(e).__name__} {e}"})
                        continue
                    if args.commit_each:
                        conn.commit()
                    got += 1
                # 音频与听力题号的对应关系：两件事都要说清楚，不能只报"缺音频"
                if not info.get("kaoyan"):
                    if n_listening == 0:
                        errors.append({
                            "file": rel, "question_no": "-", "field": "question_no",
                            "level": "warn",
                            "reason": f"未切出任何听力题号（{CET_LISTENING_RANGE[0]}-"
                                      f"{CET_LISTENING_RANGE[1]}），本卷现有题号从第 "
                                      f"{min((qno_int(q['no']) for q in qs if qno_int(q['no'])), default='-')}"
                                      f" 题起——请四号确认是否本就无听力部分"})
                    elif not audio_refs:
                        stats["missing_audio"] += 1
                        errors.append({
                            "file": rel, "question_no": "-", "field": "audio_refs_json",
                            "level": "warn",
                            "reason": f"含 {n_listening} 道听力题但没有音频文件"
                                      f"——登记待确认，**未复用其他套的音频**"})
                stats["questions"] += got
                stats["papers"] += 1
                print(f"    + {f.name}：{len(text.strip())} 字 → {got} 题"
                      f"（听力题 {n_listening}）")

        if args.dry_run:
            conn.rollback()
            print("\n事务：--dry-run，未写入任何数据")
        elif args.commit_each:
            conn.commit()
            print("\n事务：逐条提交（--commit-each），已提交部分不会回滚")
        else:
            conn.commit()
            print("\n事务：整批提交成功（默认策略：任一步异常则整批回滚）")

        if not args.dry_run:
            for c in conflicts:
                cur.execute(
                    "INSERT INTO question_bank_conflicts (run_id, source_question_id,"
                    " source_file, question_no, field, reason, existing_value,"
                    " incoming_value) VALUES (?,?,?,?,?,?,?,?)",
                    (run_id, c.get("source_question_id"), c.get("source_file"),
                     c.get("question_no"), c.get("field"), c.get("reason"),
                     c.get("existing_value"), c.get("incoming_value")))
            conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"\n!! 导入异常，已整批回滚：{type(e).__name__} {e}")
        stats["failed"] += 1
        errors.append({"file": "-", "question_no": "-", "field": "-", "level": "error",
                       "reason": f"整批回滚：{type(e).__name__} {e}"})
        raise

    total = cur.execute("SELECT COUNT(*) FROM question_bank").fetchone()[0]
    conn.close()

    print("\n" + "=" * 62)
    print(f"运行批次 run_id：{run_id}")
    print(f"真题 PDF 处理 {stats['pdf']} 份，成卷 {stats['papers']} 套")
    print(f"  扫描件（未切分）：{stats['scanned']}")
    print(f"  同内容重复文件：{stats['dup_file']}")
    print(f"  缺音频（已登记待确认）：{stats['missing_audio']}")
    print(f"切出题目 {stats['questions']} 道")
    print(f"  疑似噪声跳过：{stats['notice']}    无书面题干（听力）：{stats['no_stem']}")
    print(f"  命名不规范：{stats['badname']}    写库失败：{stats['failed']}")
    if not args.dry_run:
        print(f"入库：新增 {stats['inserted']}，更新 {stats['updated']}，"
              f"因非草稿态跳过 {stats['skipped_conflict']}")
        print(f"question_bank 现有总行数：{total}")
    else:
        print("（--dry-run，未写库）")
    n_err = sum(1 for e in errors if e.get("level", "error") == "error")
    print(f"错误 {n_err} 条 / 待确认提示 {len(errors) - n_err} 条 / 冲突 {len(conflicts)} 条")

    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rpt = {"run_id": run_id, "database": str(db_path), "root": str(bank),
               "dry_run": args.dry_run,
               "txn": "per-record" if args.commit_each else "batch",
               "stats": stats, "errors": errors, "conflicts": conflicts,
               "scanned_files": scanned, "noise_samples": notices[:20]}
        rp.write_text(json.dumps(rpt, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"报告已导出：{rp.resolve()}")

    if errors:
        print(f"\n错误与待确认明细（前 10 条，共 {len(errors)}）：")
        for e in errors[:10]:
            lvl = "错误" if e.get("level", "error") == "error" else "提示"
            print(f"  - [{lvl}] {e['file']}#{e['question_no']} "
                  f"[{e['field']}] {e['reason']}")
    if scanned:
        print("\n扫描件清单（需走 OCR）：")
        for s in scanned:
            print("  -", s)


if __name__ == "__main__":
    main()
