# -*- coding: utf-8 -*-
"""端到端联调：OCR 识别文本 → AI 错题解析"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
import ai_interface

TEXT_FILE = Path(r"D:\bigcreat_project\data\ocr_fast_test\all_text.txt")

print("Ollama 在线:", ai_interface.check_ollama_alive())
question = ai_interface.ocr_result_to_question(TEXT_FILE.read_text(encoding="utf-8").splitlines())
print(f"题目文本共 {len(question)} 字符，开头：{question[:50]}...")

t0 = time.time()
resp = ai_interface.explain_wrong_question(
    question=question,
    student_answer="lim [f(x0+∆x,y0+∆y)−f(x0,y0)]/∆x，我算的结果等于 1")
print(f"AI 耗时 {time.time()-t0:.1f} 秒")
print("=" * 50)
print(resp.content)
