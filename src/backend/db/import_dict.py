# -*- coding: utf-8 -*-
"""词库导入器：把 QwertyLearner 的词库 JSON 灌进 dicts + words 表"""
import json
from pathlib import Path
from db import get_conn, DB_PATH

ROOT = Path(__file__).resolve().parents[3]
DICT_FILE = ROOT / "upstream" / "QwertyLearner" / "public" / "dicts" / "CET4_T.json"

DICT_KEY = "CET4_T"
DICT_NAME = "四级词汇"


def main():
    conn = get_conn()
    cur = conn.cursor()

    # 1. 读词库 JSON
    words = json.loads(DICT_FILE.read_text(encoding="utf-8"))
    print(f"读到 {len(words)} 个单词")

    # 2. 词库元信息（重复跑就先删旧的，外键级联会自动清掉 words）
    cur.execute("DELETE FROM dicts WHERE dict_key=?", (DICT_KEY,))
    cur.execute(
        "INSERT INTO dicts (dict_key, name, category, word_count, source_file)"
        " VALUES (?,?,?,?,?)",
        (DICT_KEY, DICT_NAME, "CET4", len(words), DICT_FILE.name),
    )
    dict_id = cur.lastrowid

    # 3. 组装数据：trans 是数组，要转成 JSON 字符串才能存进 TEXT 列
    rows = []
    for i, w in enumerate(words):
        rows.append((
            dict_id,
            w.get("name", ""),
            json.dumps(w.get("trans", []), ensure_ascii=False),
            w.get("usphone", ""),
            w.get("ukphone", ""),
            0,   # chapter
            i,   # order_index：词库里的原始顺序
        ))

    # 4. 批量插入（executemany 一次搞定，别写 for 循环单条插）
    cur.executemany(
        "INSERT INTO words (dict_id, word, trans, usphone, ukphone, chapter, order_index)"
        " VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()

    # 5. 验证
    print("dicts 条数:", cur.execute("SELECT COUNT(*) FROM dicts").fetchone()[0])
    print("words 条数:", cur.execute("SELECT COUNT(*) FROM words").fetchone()[0])
    print("前 3 个单词：")
    for r in cur.execute(
        "SELECT word, usphone, trans FROM words WHERE dict_id=? ORDER BY order_index LIMIT 3",
        (dict_id,)):
        print(f"  {r['word']:12s} /{r['usphone']}/  {r['trans']}")

    conn.close()
    print("词库导入完成，数据库:", DB_PATH)


if __name__ == "__main__":
    main()
