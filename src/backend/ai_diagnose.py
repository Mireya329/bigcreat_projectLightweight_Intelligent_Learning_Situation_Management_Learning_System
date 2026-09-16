# -*- coding: utf-8 -*-
"""AI 薄弱点诊断：把跨线学情数据喂给本地大模型，生成诊断与复习建议。

数据流：
  SQLite（单词线 + 错题线）
    → study_stats.collect_stats() 采集结构化统计
    → ai_interface.diagnose_weakness() 送本地 Ollama
    → 打印 + 写入 ai_logs + 保存 Markdown 报告

注意：诊断质量取决于模型。0.5b 只能验证链路和格式，
真正可用的诊断需要 7b 及以上（任务 B/条目 17 的实测结论）。

用法：python src/backend/ai_diagnose.py [用户名]
"""
import json
import sys
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "db"))

import ai_interface                              # noqa: E402
from db import get_conn, DB_PATH                 # noqa: E402
from study_stats import collect_stats            # noqa: E402

REPORT = HERE.parent.parent / "docs" / "学情诊断报告.md"


def main():
    username = sys.argv[1] if len(sys.argv) > 1 else "test"
    conn = get_conn()
    cur = conn.cursor()
    uid = cur.execute(
        "SELECT id FROM users WHERE username=?", (username,)).fetchone()[0]

    stats = collect_stats(cur, uid)
    print("=" * 58)
    print(f"AI 薄弱点诊断 · 用户 {username}")
    print("=" * 58)
    print("\n feeding AI 的学情数据：")
    print(json.dumps(stats, ensure_ascii=False, indent=1))

    if not ai_interface.check_ollama_alive():
        print("\n⚠️ Ollama 不在线，无法生成诊断。")
        print("   启动方式：D:\\Ollama\\ollama.exe serve（或桌面'启动Ollama.bat'）")
        conn.close()
        return

    print(f"\nOllama 在线，模型 {ai_interface.MODEL_NAME}，生成中...")
    resp = ai_interface.diagnose_weakness(stats)
    print(f"\n耗时 {resp.elapsed_ms/1000:.1f} 秒\n")
    print("=" * 58)
    print(resp.content)
    print("=" * 58)

    # 记日志：诊断同样走 ai_logs，方便横向对比不同模型的效果
    cur.execute(
        "INSERT INTO ai_logs"
        " (user_id, scene, model, prompt_chars, response, elapsed_ms, ok)"
        " VALUES (?,?,?,?,?,?,?)",
        (uid, "diagnose", ai_interface.MODEL_NAME,
         len(json.dumps(stats, ensure_ascii=False)), resp.content,
         resp.elapsed_ms, 1))
    conn.commit()

    # 存一份 Markdown 报告，可直接给组长/老师看
    REPORT.write_text(
        f"# 学情诊断报告\n\n"
        f"- 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
        f"- 用户：{username}\n"
        f"- 模型：{ai_interface.MODEL_NAME}（耗时 {resp.elapsed_ms/1000:.1f} 秒）\n\n"
        f"## 输入数据\n\n```json\n"
        f"{json.dumps(stats, ensure_ascii=False, indent=1)}\n```\n\n"
        f"## AI 诊断输出\n\n{resp.content}\n",
        encoding="utf-8")
    print(f"\n报告已保存：{REPORT}")
    print("数据库:", DB_PATH)
    conn.close()


if __name__ == "__main__":
    main()
