# -*- coding: utf-8 -*-
"""从 Anki 导出的 CSV 把卡片导回我们的库（反向同步：Anki 桌面端 → 我们）。

配套 `anki_export.py` 完成双向：
    我们 --anki_export.py--> CSV --Anki 导入--> Anki 桌面端
    Anki 桌面端 --导出卡片为 CSV--> our CSV --anki_import_csv.py--> 我们

解析规则：
  - 自动跳过标题行（含"正面"字样的第一行）
  - 兼容 utf-8 / utf-8-sig / gbk 三种编码（Anki 导出可能是带 BOM 的）
  - 取**前两列**作为正面/背面，多余的列忽略
  - 按 front 去重，已存在则跳过（幂等）

用法：
    python src/backend/anki_import_csv.py 路径.csv
    python src/backend/anki_import_csv.py 路径.csv --apply    # 默认演练
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn, DB_PATH          # noqa: E402

USERNAME = "test"


def read_csv(path: Path):
    """兼容三种编码读取 CSV，返回行列表"""
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            with open(path, encoding=enc, newline="") as f:
                return list(csv.reader(f)), enc
        except UnicodeDecodeError:
            continue
    raise SystemExit(f"无法解码文件：{path}（试过 utf-8-sig / utf-8 / gbk）")


def main():
    argv = sys.argv[1:]
    if not argv or argv[0].startswith("--"):
        print("用法：python src/backend/anki_import_csv.py 路径.csv [--apply]")
        return
    path = Path(argv[0])
    apply_mode = "--apply" in argv

    rows, enc = read_csv(path)
    print(f"读到 {len(rows)} 行（编码 {enc}）")

    conn = get_conn()
    cur = conn.cursor()
    uid = cur.execute(
        "SELECT id FROM users WHERE username=?", (USERNAME,)).fetchone()[0]

    added = skipped = 0
    for r in rows:
        if not r or not r[0].strip():
            continue
        if r[0].strip() == "正面":          # 跳过标题行
            continue
        front = r[0].strip()
        back = r[1].strip() if len(r) > 1 else ""

        exist = cur.execute(
            "SELECT COUNT(*) FROM anki_cards WHERE user_id=? AND front=?",
            (uid, front)).fetchone()[0]
        if exist:
            skipped += 1
            continue
        if apply_mode:
            cur.execute(
                "INSERT INTO anki_cards (user_id, front, back) VALUES (?,?,?)",
                (uid, front, back))
        added += 1

    if apply_mode:
        conn.commit()

    print(f"可导入 {added} 张，已存在跳过 {skipped} 张")
    print("[演练模式] 加 --apply 写入" if not apply_mode else "已写入数据库")
    n = cur.execute("SELECT COUNT(*) FROM anki_cards WHERE user_id=?",
                    (uid,)).fetchone()[0]
    print(f"卡片总数 {n}，数据库 {DB_PATH}")
    conn.close()


if __name__ == "__main__":
    main()
