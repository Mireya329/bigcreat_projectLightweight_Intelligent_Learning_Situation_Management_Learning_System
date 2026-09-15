"""真实试卷 OCR 终极版 v2：每片独立进程，失败不影响"""
import os, sys, time, json, subprocess
os.environ['FLAGS_use_mkldnn'] = '0'

import shutil
from pathlib import Path
from PIL import Image

img_path = Path('data/test_real_exam.png').resolve()
print(f'图片: {img_path}', flush=True)

img = Image.open(img_path).convert('RGB')
w0, h0 = img.size
print(f'原图: {w0}x{h0}', flush=True)

SAFE_W = 1280
SAFE_H = 300

# 缩到 SAFE_W 宽
if w0 > SAFE_W:
    scale = SAFE_W / w0
    new_w, new_h = int(w0 * scale), int(h0 * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    w0, h0 = new_w, new_h
print(f'缩到: {w0}x{h0}', flush=True)

# 保存缩放后的图
scaled_path = Path('data/test_real_exam_scaled.png')
img.save(scaled_path)
print(f'保存缩放图: {scaled_path}', flush=True)

out_dir = Path(r'D:\bigcreat_project\data\ocr_real_test')
out_dir.mkdir(parents=True, exist_ok=True)
slices_out_dir = out_dir / 'slices'
slices_out_dir.mkdir(parents=True, exist_ok=True)

# 切片并保存为单独文件
slices_meta = []
for i, top in enumerate(range(0, h0, SAFE_H)):
    bottom = min(top + SAFE_H, h0)
    slice_path = slices_out_dir / f'slice_{i:02d}.png'
    img.crop((0, top, w0, bottom)).save(slice_path)
    slices_meta.append((i, top, bottom, slice_path))
print(f'切片数: {len(slices_meta)}', flush=True)

# 每个切片用独立 Python 进程跑 OCR
worker_path = r'D:\bigcreat_project\src\backend\ocr_one_slice.py'
python_exe = r'D:\bigcreat_project\venv\Scripts\python.exe'

t_start = time.time()
results = []
for idx, top, bottom, slice_path in slices_meta:
    out_json = out_dir / f'slice_{idx:02d}.json'
    cmd = [python_exe, '-u', worker_path, str(slice_path), str(out_json)]
    print(f'\n--- 切片 {idx+1}/{len(slices_meta)} (y={top}~{bottom}) ---', flush=True)
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore', timeout=300)
        dt = time.time() - t0
        if proc.returncode != 0:
            print(f'  FAIL exit={proc.returncode} after {dt:.1f}s', flush=True)
            if proc.stderr:
                print(f'  stderr: {proc.stderr[:150]}', flush=True)
            continue
        if out_json.exists():
            with open(out_json, 'r', encoding='utf-8') as f:
                data = json.load(f)
            n = len(data.get('items', []))
            print(f'  OK {n} lines in {dt:.1f}s (worker_time={data.get("time", 0):.1f}s)', flush=True)
            results.append({'slice': idx, 'y_offset': top, 'items': data['items']})
            # 打印前 5 条预览
            for it in data['items'][:5]:
                print(f'    [{it["score"]:.3f}] {it["text"]}', flush=True)
        else:
            print(f'  No output file', flush=True)
    except subprocess.TimeoutExpired:
        print(f'  TIMEOUT after 300s', flush=True)
    except Exception as e:
        print(f'  ERROR: {type(e).__name__}: {str(e)[:80]}', flush=True)

t_total = time.time() - t_start
all_items = [it for r in results for it in r['items']]
scores = [x['score'] for x in all_items]

print(f'\n=== 总汇总 ===', flush=True)
print(f'总耗时（含 N 次进程启动和模型加载）: {t_total:.1f}s', flush=True)
print(f'总识别行数: {len(all_items)}', flush=True)
if scores:
    print(f'平均置信度: {sum(scores)/len(scores):.3f}', flush=True)
    print(f'高置信(>0.9): {sum(1 for s in scores if s > 0.9)}/{len(scores)} ({100*sum(1 for s in scores if s > 0.9)/len(scores):.0f}%)', flush=True)
    print(f'低置信(<0.7): {sum(1 for s in scores if s < 0.7)}/{len(scores)} ({100*sum(1 for s in scores if s < 0.7)/len(scores):.0f}%)', flush=True)
    print(f'中置信: {sum(1 for s in scores if 0.7 <= s <= 0.9)}/{len(scores)}', flush=True)

# 保存总结果
final = {
    'image': str(img_path),
    'safe_w': SAFE_W, 'safe_h': SAFE_H,
    'n_slices': len(slices_meta),
    'n_success': len(results),
    'total_time': t_total,
    'items': [{'slice': r['slice'], 'y_offset': r['y_offset'], **it} for r in results for it in r['items']],
    'stats': {
        'total_lines': len(scores),
        'avg_score': sum(scores)/len(scores) if scores else 0,
        'high_conf': sum(1 for s in scores if s > 0.9),
        'low_conf': sum(1 for s in scores if s < 0.7),
        'mid_conf': sum(1 for s in scores if 0.7 <= s <= 0.9),
    }
}
with open(out_dir / 'result_real_exam.json', 'w', encoding='utf-8') as f:
    json.dump(final, f, ensure_ascii=False, indent=2)

txt = out_dir / 'recognized_text.txt'
with open(txt, 'w', encoding='utf-8') as f:
    f.write(f'图片: {img_path}\n')
    f.write(f'原图: {Image.open(img_path).size}\n')
    f.write(f'切片数: {len(slices_meta)}, 成功: {len(results)}, 切片高度: {SAFE_H}\n')
    f.write(f'总耗时: {t_total:.1f}s, 总识别行数: {len(all_items)}\n')
    if scores:
        f.write(f'平均置信度: {sum(scores)/len(scores):.3f}\n')
    else:
        f.write('平均置信度: N/A\n')
    f.write('=' * 60 + '\n')
    for it in final['items']:
        f.write(f'[{it["score"]:.3f}] (切片{it["slice"]+1}) {it["text"]}\n')
print(f'\n完整结果: {out_dir / "result_real_exam.json"}', flush=True)
print(f'纯文本: {txt}', flush=True)