"""单切片 OCR worker（独立进程）"""
import os
os.environ['FLAGS_use_mkldnn'] = '0'
import sys, json, time
from pathlib import Path
from PIL import Image
import numpy as np
from paddleocr import PaddleOCR

img_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])

img = Image.open(img_path).convert('RGB')
arr = np.array(img)
print(f'图片: {img_path}, shape: {arr.shape}', flush=True)

ocr = PaddleOCR(use_textline_orientation=True, lang='ch', enable_mkldnn=False)
print('init done', flush=True)
t0 = time.time()
result = ocr.predict(arr)
dt = time.time() - t0

items = []
for r in result:
    for t, s, b in zip(r['rec_texts'], r['rec_scores'], r['rec_boxes']):
        items.append({'text': t, 'score': float(s), 'box': b.tolist()})

with open(out_path, 'w', encoding='utf-8') as f:
    json.dump({'image': str(img_path), 'shape': list(arr.shape), 'time': dt, 'items': items}, f, ensure_ascii=False, indent=2)
print(f'OK {len(items)} lines in {dt:.1f}s -> {out_path}', flush=True)