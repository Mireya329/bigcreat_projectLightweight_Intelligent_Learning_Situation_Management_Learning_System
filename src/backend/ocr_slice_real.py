"""真实试卷 OCR 测试（切片版）：绕开 oneDNN 在大图上的崩溃"""
import os
os.environ['FLAGS_use_mkldnn'] = '0'

import sys, time, json
from pathlib import Path
from PIL import Image
import numpy as np
from paddleocr import PaddleOCR

img_path = Path(sys.argv[1] if len(sys.argv) > 1 else 'data/test_real_exam.png').resolve()
print(f'图片: {img_path}', flush=True)

img = Image.open(img_path).convert('RGB')
w, h = img.size
print(f'原图尺寸: {w}x{h}', flush=True)

# 切片策略：长边 < 1500 像素（远小于触发 oneDNN bug 的 3893）
SLICE_H = 1400
slices_meta = []
for i, top in enumerate(range(0, h, SLICE_H)):
    bottom = min(top + SLICE_H, h)
    slices_meta.append((i, top, bottom))
print(f'切片数: {len(slices_meta)}（每片 {SLICE_H}px）', flush=True)

ocr = PaddleOCR(use_textline_orientation=True, lang='ch', enable_mkldnn=False)
print('OCR 初始化完成', flush=True)

t_start = time.time()
all_items = []
for idx, top, bottom in slices_meta:
    crop = img.crop((0, top, w, bottom))
    arr = np.array(crop)
    t0 = time.time()
    result = ocr.predict(arr)
    dt = time.time() - t0
    print(f'\n--- 切片 {idx} (y={top}~{bottom}) 耗时 {dt:.2f}s ---', flush=True)
    for r in result:
        for t, s, b in zip(r['rec_texts'], r['rec_scores'], r['rec_boxes']):
            all_items.append({'slice': idx, 'y_offset': top, 'text': t,
                              'score': float(s), 'box_y': [float(x) for x in b[0]]})
            print(f'  [{s:.3f}] {t}', flush=True)

t_total = time.time() - t_start
print(f'\n=== 汇总 ===', flush=True)
print(f'总耗时(含模型加载): {t_total:.2f}s', flush=True)
print(f'总识别行数: {len(all_items)}', flush=True)
scores = [x['score'] for x in all_items]
if scores:
    print(f'平均置信度: {sum(scores)/len(scores):.3f}', flush=True)
    print(f'高置信(>0.9): {sum(1 for s in scores if s > 0.9)}/{len(scores)}', flush=True)
    print(f'低置信(<0.7): {sum(1 for s in scores if s < 0.7)}/{len(scores)}', flush=True)

out_dir = Path(r'D:\bigcreat_project\data\ocr_real_test')
out_dir.mkdir(parents=True, exist_ok=True)
out_path = out_dir / 'result_real_exam.json'
with open(out_path, 'w', encoding='utf-8') as f:
    json.dump({
        'image': str(img_path),
        'size': [w, h],
        'slice_height': SLICE_H,
        'n_slices': len(slices_meta),
        'total_time': t_total,
        'items': all_items,
        'stats': {
            'total_lines': len(scores),
            'avg_score': sum(scores)/len(scores) if scores else 0,
            'high_conf': sum(1 for s in scores if s > 0.9),
            'low_conf': sum(1 for s in scores if s < 0.7),
            'mid_conf': sum(1 for s in scores if 0.7 <= s <= 0.9),
        }
    }, f, ensure_ascii=False, indent=2)
print(f'\n完整结果已保存: {out_path}', flush=True)