# -*- coding: utf-8 -*-
"""单词练习记录导入器：把 QwertyLearner 的练习数据灌进 word_records / chapter_records。

两种模式：
  1. 真实数据：导入 QwertyLearner 导出的 Dexie JSON
     python src/backend/word_import.py --file 用户数据.json        （解压后的 JSON）
     python src/backend/word_import.py --file 用户数据.json.gz --gz（gzip 压缩包）
  2. 演示数据：库里还没有真实练习数据时，先造一批跑通链路
     python src/backend/word_import.py --mock 200

QwertyLearner 的导出结构（dexie-export-import 标准）：
  { "data": { "tables": [ {"name": "wordRecords", "rows": [...] } ] } }
  每条 wordRecord：{word, timeStamp, dict, chapter, timing[], wrongCount, mistakes{}}
对应本项目 word_records 表：见下方 SQL。
"""
import gzip
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn, DB_PATH          # noqa: E402

USERNAME = "test"
DICT_KEY = "CET4_T"


def load_rows(path: Path, gz: bool):
    """从 Dexie 导出文件里取出 wordRecords / chapterRecords 的 rows"""
    raw = gzip.open(path, "rt", encoding="utf-8").read() if gz else \
        path.read_text(encoding="utf-8")
    data = json.loads(raw)
    tables = {t["name"]: t.get("rows", [])
              for t in data.get("data", {}).get("tables", [])}
    return tables.get("wordRecords", []), tables.get("chapterRecords", [])


def mock_rows(conn, n):
    """造演示数据：从 CET4 词库随机抽词，模拟打字练习"""
    words = conn.execute(
        "SELECT id, word FROM words WHERE dict_id="
        "(SELECT id FROM dicts WHERE dict_key=?) ORDER BY RANDOM() LIMIT ?",
        (DICT_KEY, n)).fetchall()
    rows = []
    for w in words:
        length = len(w["word"])
        timing = [random.randint(80, 400) for _ in range(length)]
        wrong = random.choices([0, 0, 0, 1, 2, 3], k=1)[0]
        mistakes = {}
        if wrong:
            idx = random.randrange(length)
            mistakes[str(idx)] = [random.choice("abcdefghijklmnopqrstuvwxyz")
                                  for _ in range(wrong)]
        rows.append({"word": w["word"], "timeStamp": 0, "dict": DICT_KEY,
                     "chapter": 0, "timing": timing, "wrongCount": wrong,
                     "mistakes": mistakes})
    return rows, []


def main():
    conn = get_conn()
    cur = conn.cursor()
    user_id = cur.execute(
        "SELECT id FROM users WHERE username=?", (USERNAME,)).fetchone()[0]

    if "--file" in sys.argv:
        p = Path(sys.argv[sys.argv.index("--file") + 1])
        gz = "--gz" in sys.argv
        wrows, crows = load_rows(p, gz)
        print(f"从 {p.name} 读到 wordRecords {len(wrows)} 条、"
              f"chapterRecords {len(crows)} 条")
    else:
        n = 200
        if "--mock" in sys.argv:
            n = int(sys.argv[sys.argv.index("--mock") + 1])
        wrows, crows = mock_rows(conn, n)
        print(f"[演示数据] 生成 {len(wrows)} 条练习记录（真实数据请用 --file）")

    # 先清掉这个用户的旧记录，保证可重复运行
    cur.execute("DELETE FROM word_records WHERE user_id=?", (user_id,))
    cur.execute("DELETE FROM chapter_records WHERE user_id=?", (user_id,))

    # 建词->id 映射，让记录能关联到 words 表
    wmap = {r["word"]: r["id"] for r in cur.execute(
        "SELECT id, word FROM words")}

    batch, missing = [], 0
    for r in wrows:
        wid = wmap.get(r.get("word", ""))
        if wid is None:
            missing += 1                      # 词库里没有的词，仍记录但无 word_id
        timing = r.get("timing") or []
        batch.append((
            user_id, wid, r.get("dict", DICT_KEY), r.get("chapter", 0),
            json.dumps(timing, ensure_ascii=False),
            r.get("wrongCount", 0),
            json.dumps(r.get("mistakes", {}), ensure_ascii=False),
            sum(timing) if timing else None,
        ))

    cur.executemany(
        "INSERT INTO word_records"
        " (user_id, word_id, dict_key, chapter, timing, wrong_count,"
        "  mistakes, total_time_ms)"
        " VALUES (?,?,?,?,?,?,?,?)", batch)

    cb = []
    for r in crows:
        cb.append((user_id, r.get("dict", DICT_KEY), r.get("chapter", 0),
                   r.get("time"), r.get("correctCount", 0),
                   r.get("wrongCount", 0), r.get("wordCount", 0),
                   json.dumps(r.get("correctWordIndexes", []),
                              ensure_ascii=False),
                   r.get("wordNumber")))
    if cb:
        cur.executemany(
            "INSERT INTO chapter_records"
            " (user_id, dict_key, chapter, duration_sec, correct_count,"
            "  wrong_count, word_count, correct_word_indexes, word_number)"
            " VALUES (?,?,?,?,?,?,?,?,?)", cb)

    conn.commit()

    n = cur.execute(
        "SELECT COUNT(*) FROM word_records WHERE user_id=?",
        (user_id,)).fetchone()[0]
    with_id = cur.execute(
        "SELECT COUNT(*) FROM word_records WHERE user_id=? AND word_id IS NOT NULL",
        (user_id,)).fetchone()[0]
    print(f"已导入 word_records {n} 条（其中 {with_id} 条已关联词库词条"
          f"，{missing} 条词库中没有）")
    print("chapter_records:", cur.execute(
        "SELECT COUNT(*) FROM chapter_records").fetchone()[0], "条")
    print("数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
