"""OCR 测试 wrapper：在 paddle 导入前彻底关 oneDNN"""
import os
# 必须在任何 paddle 导入前设置
os.environ['FLAGS_use_mkldnn'] = '0'
os.environ['FLAGS_enable_mkldnn'] = '0'
os.environ['GLOG_v'] = '0'  # 减少日志噪音

import sys
from paddleocr import PaddleOCR
from PIL import Image
import numpy as np
import time, json
from pathlib import Path

img_path = Path(sys.argv[1] if len(sys.argv) > 1 else 'data/test_real_exam.png').resolve()
print(f'图片: {img_path}', flush=True)
img = Image.open(img_path).convert('RGB')
arr = np.array(img)
print(f'shape: {arr.shape}', flush=True)

ocr = PaddleOCR(
    use_doc_orientation_classify=False,
    use_textline_orientation=False,
    lang='ch',
    enable_mkldnn=False,
)
print('init done', flush=True)

t0 = time.time()
result = ocr.predict(arr)
t_infer = time.time() - t0
print(f'predict done in {t_infer:.2f}s', flush=True)

items = []
for r in result:
    for t, s in zip(r['rec_texts'], r['rec_scores']):
        items.append({'text': t, 'score': float(s)})
        print(f'  [{s:.3f}] {t}', flush=True)

print(f'\n总行数: {len(items)}', flush=True)
if items:
    scores = [x['score'] for x in items]
    print(f'平均置信度: {sum(scores)/len(scores):.3f}', flush=True)
    print(f'高置信(>0.9): {sum(1 for s in scores if s > 0.9)}/{len(scores)}', flush=True)
    print(f'低置信(<0.7): {sum(1 for s in scores if s < 0.7)}/{len(scores)}', flush=True)

# 保存
out = Path(r'D:\bigcreat_project\data\ocr_real_test\result.json')
out.parent.mkdir(parents=True, exist_ok=True)
with open(out, 'w', encoding='utf-8') as f:
    json.dump({'image': str(img_path), 'infer_time': t_infer,
               'items': items,
               'stats': {'lines': len(items),
                         'avg': sum(x['score'] for x in items)/max(len(items),1),
                         'high': sum(1 for x in items if x['score']>0.9),
                         'low': sum(1 for x in items if x['score']<0.7)}}, f, ensure_ascii=False, indent=2)
print(f'结果: {out}', flush=True)