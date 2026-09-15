
# -*- coding: utf-8 -*-
"""整卷快速识别：单进程串行 + mobile检测/medium识别 + 1280限制"""
import json, time
from pathlib import Path
from PIL import Image
import numpy as np
from paddleocr import PaddleOCR

IMG = Path(r"D:\bigcreat_project\data\test_real_exam_scaled.png")
OUT_DIR = Path(r"D:\bigcreat_project\data\ocr_fast_test")
OUT_DIR.mkdir(exist_ok=True)
SLICE_H = 300

img = Image.open(IMG).convert("RGB")
w, h = img.size
n = (h + SLICE_H - 1) // SLICE_H
print(f"原图 {w}x{h}，切 {n} 片")

ocr = PaddleOCR(use_textline_orientation=True, lang="ch", enable_mkldnn=False,
                text_detection_model_name="PP-OCRv5_mobile_det",
                text_recognition_model_name="PP-OCRv6_medium_rec",
                text_det_limit_side_len=1280, text_det_limit_type="max")

all_lines = []
t_start = time.time()
for i in range(n):
    top = i * SLICE_H
    bottom = min(h, top + SLICE_H)
    arr = np.array(img.crop((0, top, w, bottom)))
    t0 = time.time()
    result = ocr.predict(arr)
    dt = time.time() - t0
    items = []
    for r in result:
        for t, s, b in zip(r["rec_texts"], r["rec_scores"], r["rec_boxes"]):
            items.append({"text": t, "score": float(s), "box": b.tolist()})
            all_lines.append(t)
    out = OUT_DIR / f"slice_{i:02d}.json"
    out.write_text(json.dumps({"slice": i, "time": dt, "items": items},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"切片 {i:02d}: {len(items)} 行, {dt:.2f} 秒")

total = time.time() - t_start
print("=" * 50)
print(f"整卷总耗时: {total:.2f} 秒（单进程串行，含一次模型加载）")
print(f"总识别行数: {len(all_lines)}")
(OUT_DIR / "all_text.txt").write_text("\n".join(all_lines), encoding="utf-8")
print(f"文本已存: {OUT_DIR / 'all_text.txt'}")
