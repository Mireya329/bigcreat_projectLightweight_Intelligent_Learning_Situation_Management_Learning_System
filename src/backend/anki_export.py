# -*- coding: utf-8 -*-
"""把我们的卡片导出成 Anki 可导入的 CSV（正向同步：我们 → Anki 桌面端）。

为什么用 CSV 而不是直接读写 collection.anki2：
  1. 本机未安装 Anki，没有 collection 数据库可操作
  2. 直连 Anki 数据库要求 Anki 完全关闭，且 schema 版本升级会失效，风险高
  3. Anki 桌面端原生支持「文件 → 导入 → CSV」，**零依赖、零插件、最稳**

导出后用 utf-8-sig（带 BOM），这样用 Excel 打开中文不会乱码。

用法：
    python src/backend/anki_export.py                 # 导出全部卡片
    python src/backend/anki_export.py --due-only      # 只导出今日到期
    python src/backend/anki_export.py --out 路径.csv
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "db"))
from db import get_conn, DB_PATH          # noqa: E402

USERNAME = "test"
# 直接用 db.py 里的库文件路径推导项目根，避免自己数 parents 层数
# （这个 bug 已经犯过三次：src/backend/ 下的脚本要退三级才到项目根）
DEFAULT_OUT = DB_PATH.parent / "anki_export.csv"


def main():
    out = DEFAULT_OUT
    if "--out" in sys.argv:
        out = Path(sys.argv[sys.argv.index("--out") + 1])
    due_only = "--due-only" in sys.argv

    conn = get_conn()
    cur = conn.cursor()
    uid = cur.execute(
        "SELECT id FROM users WHERE username=?", (USERNAME,)).fetchone()[0]

    sql = ("SELECT front, back, ef, interval_days, repetitions, due_date"
           " FROM anki_cards WHERE user_id=?")
    if due_only:
        sql += " AND due_date <= date('now','localtime')"
    sql += " ORDER BY id"
    rows = cur.execute(sql, (uid,)).fetchall()
    conn.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig：带 BOM，Excel 打开不乱码
    with open(out, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["正面", "背面", "难度因子", "间隔天数", "连续答对", "下次到期"])
        for r in rows:
            back = (r["back"] or "").replace("\n", " ")
            w.writerow([r["front"], back, r["ef"], r["interval_days"],
                        r["repetitions"], r["due_date"]])

    print(f"已导出 {len(rows)} 张卡片 → {out}")
    print("用法：Anki 桌面端 → 文件 → 导入 → 选这个 CSV → 字段分隔符选「逗号」")
    if due_only:
        print("（本次只导出今日到期的卡片）")


if __name__ == "__main__":
    main()
