"""汇总已完成的切片识别结果"""
import json
from pathlib import Path
from PIL import Image

out_dir = Path(r'D:\bigcreat_project\data\ocr_real_test')
slice_files = sorted(out_dir.glob('slice_*.json'))
print(f'完成的切片数: {len(slice_files)}')

all_items = []
results = []
for sf in slice_files:
    with open(sf, 'r', encoding='utf-8') as f:
        data = json.load(f)
    idx = int(sf.stem.split('_')[1])
    items = data.get('items', [])
    results.append({'slice': idx, 'items': items, 'time': data.get('time', 0)})
    for it in items:
        all_items.append({'slice': idx, 'score': it['score'], 'text': it['text']})

print(f'总识别行数: {len(all_items)}')
scores = [x['score'] for x in all_items]
print(f'平均置信度: {sum(scores)/len(scores):.3f}')
print(f'高置信(>0.9): {sum(1 for s in scores if s > 0.9)}/{len(scores)} ({100*sum(1 for s in scores if s > 0.9)/len(scores):.1f}%)')
print(f'中置信: {sum(1 for s in scores if 0.7 <= s <= 0.9)}/{len(scores)} ({100*sum(1 for s in scores if 0.7 <= s <= 0.9)/len(scores):.1f}%)')
print(f'低置信(<0.7): {sum(1 for s in scores if s < 0.7)}/{len(scores)} ({100*sum(1 for s in scores if s < 0.7)/len(scores):.1f}%)')

# 按切片分组输出
print(f'\n=== 按切片分布 ===')
for r in sorted(results, key=lambda x: x['slice']):
    print(f'切片 {r["slice"]:02d}: {len(r["items"])} 行 (worker耗时 {r["time"]:.1f}s)')

# 输出前 30 条预览
print(f'\n=== 识别结果预览（前 30 条） ===')
for it in all_items[:30]:
    print(f'  [切片{it["slice"]+1:02d}] [{it["score"]:.3f}] {it["text"]}')

# 保存汇总
final = {
    'image': 'D:\\bigcreat_project\\data\\test_real_exam.png',
    'image_size': Image.open('D:\\bigcreat_project\\data\\test_real_exam.png').size,
    'slice_height': 300,
    'n_slices_total': 13,
    'n_slices_done': len(slice_files),
    'items': [{'slice': it['slice'], 'score': it['score'], 'text': it['text']} for it in all_items],
    'stats': {
        'total_lines': len(scores),
        'avg_score': sum(scores)/len(scores),
        'high_conf': sum(1 for s in scores if s > 0.9),
        'mid_conf': sum(1 for s in scores if 0.7 <= s <= 0.9),
        'low_conf': sum(1 for s in scores if s < 0.7),
    }
}
with open(out_dir / 'result_real_exam.json', 'w', encoding='utf-8') as f:
    json.dump(final, f, ensure_ascii=False, indent=2)

# 纯文本
txt = out_dir / 'recognized_text.txt'
with open(txt, 'w', encoding='utf-8') as f:
    f.write(f'图片: D:\\bigcreat_project\\data\\test_real_exam.png\n')
    f.write(f'原图: 1280x3893\n')
    f.write(f'切片高度: 300px\n')
    f.write(f'完成切片: {len(slice_files)}/13\n')
    f.write(f'总识别行数: {len(all_items)}\n')
    f.write(f'平均置信度: {sum(scores)/len(scores):.3f}\n')
    f.write(f'高置信率: {100*sum(1 for s in scores if s > 0.9)/len(scores):.1f}%\n')
    f.write('=' * 60 + '\n')
    for it in all_items:
        f.write(f'[{it["score"]:.3f}] (切片{it["slice"]+1}) {it["text"]}\n')

print(f'\n完整结果: {out_dir / "result_real_exam.json"}')
print(f'纯文本: {txt}')