"""真实试卷 OCR 终极版：安全尺寸分片"""
import os, sys, time, json, subprocess
os.environ['FLAGS_use_mkldnn'] = '0'
from pathlib import Path
from PIL import Image
import numpy as np
from paddleocr import PaddleOCR

img_path = Path('data/test_real_exam.png').resolve()
print(f'图片: {img_path}', flush=True)

img = Image.open(img_path).convert('RGB')
w0, h0 = img.size
print(f'原图: {w0}x{h0}', flush=True)

# 已知安全尺寸：1280x300（已测试通过）
SAFE_W = 1280
SAFE_H = 300

# 缩到 SAFE_W 宽
if w0 > SAFE_W:
    scale = SAFE_W / w0
    new_w, new_h = int(w0 * scale), int(h0 * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    w0, h0 = new_w, new_h
    print(f'缩到: {w0}x{h0}', flush=True)

# 按 SAFE_H 切片
slices = []
for i, top in enumerate(range(0, h0, SAFE_H)):
    bottom = min(top + SAFE_H, h0)
    slices.append((i, top, bottom))
print(f'切片数: {len(slices)}', flush=True)

# OCR 初始化
ocr = PaddleOCR(use_textline_orientation=True, lang='ch', enable_mkldnn=False)
print('OCR init done', flush=True)

t_start = time.time()
all_items = []
for idx, top, bottom in slices:
    crop = img.crop((0, top, w0, bottom))
    arr = np.array(crop)
    print(f'\n--- 切片 {idx+1}/{len(slices)} (y={top}~{bottom}) shape={arr.shape} ---', flush=True)
    try:
        t0 = time.time()
        result = ocr.predict(arr)
        dt = time.time() - t0
        n = sum(len(x['rec_texts']) for x in result)
        print(f'  OK {n} lines in {dt:.1f}s', flush=True)
        for r in result:
            for t, s in zip(r['rec_texts'], r['rec_scores']):
                all_items.append({'slice': idx, 'y_offset': top, 'text': t, 'score': float(s)})
                print(f'  [{s:.3f}] {t}', flush=True)
    except Exception as e:
        print(f'  FAIL: {type(e).__name__}: {str(e)[:100]}', flush=True)
        # 单次失败就跳过，继续下一片
        continue

t_total = time.time() - t_start
print(f'\n=== 汇总 ===', flush=True)
print(f'总耗时: {t_total:.1f}s', flush=True)
print(f'总识别行数: {len(all_items)}', flush=True)
scores = [x['score'] for x in all_items]
if scores:
    print(f'平均置信度: {sum(scores)/len(scores):.3f}', flush=True)
    print(f'高置信(>0.9): {sum(1 for s in scores if s > 0.9)}/{len(scores)} ({100*sum(1 for s in scores if s > 0.9)/len(scores):.0f}%)', flush=True)
    print(f'低置信(<0.7): {sum(1 for s in scores if s < 0.7)}/{len(scores)} ({100*sum(1 for s in scores if s < 0.7)/len(scores):.0f}%)', flush=True)

out_dir = Path(r'D:\bigcreat_project\data\ocr_real_test')
out_dir.mkdir(parents=True, exist_ok=True)
with open(out_dir / 'result_real_exam.json', 'w', encoding='utf-8') as f:
    json.dump({
        'image': str(img_path),
        'safe_w': SAFE_W, 'safe_h': SAFE_H,
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

txt = out_dir / 'recognized_text.txt'
with open(txt, 'w', encoding='utf-8') as f:
    f.write(f'图片: {img_path}\n')
    f.write(f'尺寸: {Image.open(img_path).size} -> {w0}x{h0}\n')
    f.write(f'切片数: {len(slices)}, 切片高度: {SAFE_H}\n')
    f.write(f'总耗时: {t_total:.1f}s, 总识别行数: {len(all_items)}\n')
    f.write(f'平均置信度: {sum(scores)/len(scores):.3f}\n')
    f.write(f'高置信率: {100*sum(1 for s in scores if s > 0.9)/len(scores):.0f}%' if scores else '')
    f.write('\n' + '=' * 60 + '\n')
    for it in all_items:
        f.write(f'[{it["score"]:.3f}] {it["text"]}\n')
print(f'\n结果: {out_dir / "result_real_exam.json"}', flush=True)
print(f'纯文本: {txt}', flush=True)