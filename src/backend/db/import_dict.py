# -*- coding: utf-8 -*-
"""词库导入器：把 QwertyLearner 的词库 JSON 灌进 dicts + words 表

默认导入四级（CET4_T），与 2026-10-21 之前的行为完全一致。

四号 2026-10-21 问"六级 / 考研词库需不需要我收集"——**不需要**：
上游 QwertyLearner 已经自带，格式与 CET4_T 相同（name/trans/usphone/ukphone），
直接指定文件即可导入。可用的有：
    CET6_T(2345)  KaoYan_2024(3731)  KaoYan_3_T(3728)
    2025KaoYanHongBaoShu(6705)  KaoYanShanGuo_2023(1771)  kaoyanshanguo2025(2257)

用法：
    python src/backend/db/import_dict.py                    # 默认四级
    python src/backend/db/import_dict.py --dict CET6_T --name 六级词汇 --category CET6
"""
import argparse
import json
from pathlib import Path
from db import get_conn, DB_PATH

ROOT = Path(__file__).resolve().parents[3]
DICTS_DIR = ROOT / "upstream" / "QwertyLearner" / "public" / "dicts"

DICT_KEY = "CET4_T"
DICT_NAME = "四级词汇"
CATEGORY = "CET4"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dict", default=DICT_KEY, help="词库文件名（不含 .json）")
    ap.add_argument("--name", default=None, help="词库显示名，默认与 --dict 同名")
    ap.add_argument("--category", default=None, help="分类，默认 CET4")
    args = ap.parse_args()

    dict_key = args.dict
    dict_name = args.name or dict_key
    category = args.category or CATEGORY
    dict_file = DICTS_DIR / f"{dict_key}.json"
    if not dict_file.exists():
        raise SystemExit(f"词库文件不存在：{dict_file}\n"
                         f"可用：{', '.join(sorted(p.stem for p in DICTS_DIR.glob('*.json')))}")

    conn = get_conn()
    cur = conn.cursor()

    # 1. 读词库 JSON
    words = json.loads(dict_file.read_text(encoding="utf-8"))
    print(f"读到 {len(words)} 个单词")

    # 2. 词库元信息（重复跑就先删旧的，外键级联会自动清掉 words）
    cur.execute("DELETE FROM dicts WHERE dict_key=?", (dict_key,))
    cur.execute(
        "INSERT INTO dicts (dict_key, name, category, word_count, source_file)"
        " VALUES (?,?,?,?,?)",
        (dict_key, dict_name, category, len(words), dict_file.name),
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
