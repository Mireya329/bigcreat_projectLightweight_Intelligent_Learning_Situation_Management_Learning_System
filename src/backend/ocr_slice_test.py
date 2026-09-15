"""对超长试卷图切片识别（绕开 paddle 长图处理 bug）"""
from paddleocr import PaddleOCR
from PIL import Image
import numpy as np, json, time
from pathlib import Path

img_path = Path('data/test_real_exam.jpg')
img = Image.open(img_path).convert('RGB')
w, h = img.size
print(f'原图: {w}x{h}', flush=True)

# 每片高度不超过 1500 像素（< paddle 内部阈值）
slice_h = 1500
slices = []
for i, top in enumerate(range(0, h, slice_h)):
    bottom = min(top + slice_h, h)
    crop = img.crop((0, top, w, bottom))
    crop_path = Path(f'data/test_real_exam_part{i}.jpg')
    crop.save(crop_path, quality=92)
    slices.append(crop_path)
    print(f'  切片 {i}: y={top}~{bottom} ({bottom-top} px) -> {crop_path}', flush=True)

ocr = PaddleOCR(use_textline_orientation=True, lang='ch', enable_mkldnn=False)
all_items = []
t0 = time.time()
for idx, p in enumerate(slices):
    arr = np.array(Image.open(p).convert('RGB'))
    result = ocr.predict(arr)
    print(f'\n--- 切片 {idx} ({p.name}) 识别结果 ---', flush=True)
    for r in result:
        for t, s in zip(r['rec_texts'], r['rec_scores']):
            all_items.append({'slice': idx, 'text': t, 'score': float(s)})
            print(f'  [{s:.3f}] {t}', flush=True)
t_total = time.time() - t0

scores = [x['score'] for x in all_items]
print(f'\n=== 汇总 ===')
print(f'总耗时: {t_total:.2f}s（包含 PaddleOCR 模型加载）')
print(f'总行数: {len(scores)}, 平均置信度: {sum(scores)/len(scores):.3f}')
print(f'高置信(>0.9): {sum(1 for s in scores if s > 0.9)}/{len(scores)}')
print(f'低置信(<0.7): {sum(1 for s in scores if s < 0.7)}/{len(scores)}')

out = Path(r'D:\bigcreat_project\data\ocr_real_test\result.json')
out.parent.mkdir(parents=True, exist_ok=True)
with open(out, 'w', encoding='utf-8') as f:
    json.dump({'image': str(img_path), 'size': [w, h], 'slices': len(slices),
               'total_time': t_total, 'items': all_items,
               'stats': {'lines': len(scores), 'avg_score': sum(scores)/len(scores),
                         'high_conf': sum(1 for s in scores if s > 0.9),
                         'low_conf': sum(1 for s in scores if s < 0.7)}}, f, ensure_ascii=False, indent=2)
print(f'\n结果已保存: {out}', flush=True)