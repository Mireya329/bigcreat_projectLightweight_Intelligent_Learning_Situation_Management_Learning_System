"""真实试卷 OCR 测试（生产可用版）
策略：宽 ≤ 1280 后按 ~1400px 高度切片，逐片识别
"""
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
w0, h0 = img.size
print(f'原图: {w0}x{h0}', flush=True)

# 1) 缩到 width ≤ 1280（oneDNN 安全阈值）
MAX_W = 1280
if w0 > MAX_W:
    scale = MAX_W / w0
    new_w, new_h = int(w0 * scale), int(h0 * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    print(f'缩到: {new_w}x{new_h}', flush=True)
else:
    new_w, new_h = w0, h0

# 2) 按 1400px 高度切片（避免纵向超长）
SLICE_H = 1400
slices = []
for i, top in enumerate(range(0, new_h, SLICE_H)):
    bottom = min(top + SLICE_H, new_h)
    crop = img.crop((0, top, new_w, bottom))
    slices.append(crop)
print(f'切片数: {len(slices)}', flush=True)

ocr = PaddleOCR(use_textline_orientation=True, lang='ch', enable_mkldnn=False)
print('OCR init done', flush=True)

t_start = time.time()
all_items = []
for idx, crop in enumerate(slices):
    arr = np.array(crop)
    t0 = time.time()
    result = ocr.predict(arr)
    dt = time.time() - t0
    n = sum(len(x['rec_texts']) for x in result)
    print(f'\n--- 切片 {idx+1}/{len(slices)} ({arr.shape[1]}x{arr.shape[0]}) 耗时 {dt:.2f}s 识别 {n} 行 ---', flush=True)
    for r in result:
        for t, s, b in zip(r['rec_texts'], r['rec_scores'], r['rec_boxes']):
            all_items.append({'slice': idx, 'text': t, 'score': float(s), 'box': b.tolist()})
            print(f'  [{s:.3f}] {t}', flush=True)

t_total = time.time() - t_start
print(f'\n=== 汇总 ===', flush=True)
print(f'总耗时(含模型加载): {t_total:.1f}s', flush=True)
print(f'总识别行数: {len(all_items)}', flush=True)
scores = [x['score'] for x in all_items]
if scores:
    print(f'平均置信度: {sum(scores)/len(scores):.3f}', flush=True)
    print(f'高置信(>0.9): {sum(1 for s in scores if s > 0.9)}/{len(scores)} ({100*sum(1 for s in scores if s > 0.9)/len(scores):.0f}%)', flush=True)
    print(f'低置信(<0.7): {sum(1 for s in scores if s < 0.7)}/{len(scores)} ({100*sum(1 for s in scores if s < 0.7)/len(scores):.0f}%)', flush=True)

# 保存完整结果
out_dir = Path(r'D:\bigcreat_project\data\ocr_real_test')
out_dir.mkdir(parents=True, exist_ok=True)
with open(out_dir / 'result_real_exam.json', 'w', encoding='utf-8') as f:
    json.dump({
        'image': str(img_path),
        'original_size': [w0, h0],
        'processed_size': [new_w, new_h],
        'slice_height': SLICE_H,
        'n_slices': len(slices),
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
print(f'\n结果保存: {out_dir / "result_real_exam.json"}', flush=True)

# 保存拼接后的识别文本（方便查阅）
txt_path = out_dir / 'recognized_text.txt'
with open(txt_path, 'w', encoding='utf-8') as f:
    f.write(f'图片: {img_path}\n')
    f.write(f'尺寸: 原图 {w0}x{h0}, 处理后 {new_w}x{new_h}\n')
    f.write(f'切片数: {len(slices)}, 总耗时: {t_total:.1f}s\n')
    f.write(f'总识别行数: {len(all_items)}\n')
    f.write(f'平均置信度: {sum(scores)/len(scores):.3f}\n')
    f.write('=' * 60 + '\n')
    for i, item in enumerate(all_items):
        f.write(f'[{item["score"]:.3f}] {item["text"]}\n')
print(f'纯文本保存: {txt_path}', flush=True)