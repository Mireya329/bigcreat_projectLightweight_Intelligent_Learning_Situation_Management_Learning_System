"""真实试卷 OCR 编排器：每个切片用独立 Python 进程跑 OCR"""
import subprocess, sys, time, json
from pathlib import Path
from PIL import Image

img_path = Path('data/test_real_exam.png').resolve()
print(f'图片: {img_path}')

img = Image.open(img_path)
w, h = img.size
print(f'原图: {w}x{h}')

# 缩到 width=1280，slice 高度 950（经测试是稳定阈值）
MAX_W = 1280
SLICE_H = 950
if w > MAX_W:
    scale = MAX_W / w
    new_w, new_h = int(w * scale), int(h * scale)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    w, h = new_w, new_h
    print(f'缩到: {w}x{h}')

# 切片
slices = []
for i, top in enumerate(range(0, h, SLICE_H)):
    bottom = min(top + SLICE_H, h)
    slices.append((i, top, bottom))
print(f'切片数: {len(slices)}')

out_dir = Path(r'D:\bigcreat_project\data\ocr_real_test')
out_dir.mkdir(parents=True, exist_ok=True)

t_start = time.time()
results = []
for idx, top, bottom in slices:
    slice_json = out_dir / f'slice_{idx}.json'
    cmd = [
        r'D:\bigcreat_project\venv\Scripts\python.exe',
        r'D:\bigcreat_project\src\backend\ocr_one_slice.py',
        str(img_path), str(idx), str(top), str(bottom), str(slice_json)
    ]
    print(f'\n--- 启动切片 {idx} (y={top}~{bottom}) 子进程 ---')
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='ignore')
    if proc.returncode != 0:
        print(f'  FAIL exit={proc.returncode}')
        print(f'  stderr: {proc.stderr[:200]}')
        continue
    print(f'  stdout: {proc.stdout.strip()}')
    if slice_json.exists():
        with open(slice_json, 'r', encoding='utf-8') as f:
            data = json.load(f)
        results.append(data)

t_total = time.time() - t_start
all_items = [item for r in results for item in r['items']]
scores = [x['score'] for x in all_items]

print(f'\n=== 总汇总 ===')
print(f'总耗时（含子进程启动和模型加载）: {t_total:.1f}s')
print(f'总识别行数: {len(all_items)}')
if scores:
    print(f'平均置信度: {sum(scores)/len(scores):.3f}')
    print(f'高置信(>0.9): {sum(1 for s in scores if s > 0.9)}/{len(scores)} ({100*sum(1 for s in scores if s > 0.9)/len(scores):.0f}%)')
    print(f'低置信(<0.7): {sum(1 for s in scores if s < 0.7)}/{len(scores)} ({100*sum(1 for s in scores if s < 0.7)/len(scores):.0f}%)')
    print(f'中置信: {sum(1 for s in scores if 0.7 <= s <= 0.9)}/{len(scores)}')

# 保存最终结果
final = {
    'image': str(img_path),
    'original_size': list(Image.open(img_path).size),
    'processed_size': [w, h],
    'slice_height': SLICE_H,
    'n_slices': len(slices),
    'total_time': t_total,
    'items': [{'slice': r['slice_id'], 'y_offset': r['range'][0], **item} for r in results for item in r['items']],
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
print(f'\n完整结果: {out_dir / "result_real_exam.json"}')

# 拼接成纯文本
txt = out_dir / 'recognized_text.txt'
with open(txt, 'w', encoding='utf-8') as f:
    f.write(f'图片: {img_path}\n')
    f.write(f'原图: {Image.open(img_path).size}, 处理后: {w}x{h}\n')
    f.write(f'切片数: {len(slices)}, 总耗时: {t_total:.1f}s\n')
    f.write(f'总识别行数: {len(all_items)}\n')
    f.write(f'平均置信度: {sum(scores)/len(scores):.3f}\n')
    f.write('=' * 60 + '\n')
    for item in final['items']:
        f.write(f'[{item["score"]:.3f}] {item["text"]}\n')
print(f'纯文本: {txt}')