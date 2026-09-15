"""真实试卷 OCR 测试：性能 + 识别率统计"""
from paddleocr import PaddleOCR
import time, json, os, sys
from pathlib import Path

img_path = Path(sys.argv[1]).resolve()
print(f"图片路径: {img_path}")
print(f"是否存在: {img_path.exists()}, 大小: {img_path.stat().st_size} B")

t_start = time.time()
ocr = PaddleOCR(use_textline_orientation=True, lang='ch', enable_mkldnn=False)
t_after_load = time.time()
# 把路径转成短路径或绝对路径字符串喂给 predict
result = ocr.predict(str(img_path))
t_end = time.time()

print(f"\n=== 性能数据 ===")
print(f"模型加载耗时: {t_after_load - t_start:.2f}s")
print(f"推理耗时:     {t_end - t_after_load:.2f}s")
print(f"总耗时:       {t_end - t_start:.2f}s")
print(f"识别行数:     {sum(len(res['rec_texts']) for res in result)}")

print(f"\n=== 完整识别结果（按行）===")
all_texts = []
for res in result:
    for txt, score, box in zip(res['rec_texts'], res['rec_scores'], res['rec_boxes']):
        all_texts.append({'text': txt, 'score': float(score), 'box': box.tolist()})
        print(f"  [{score:.3f}] {txt}")

scores = [x['score'] for x in all_texts]
if scores:
    print(f"\n=== 置信度统计 ===")
    print(f"行数: {len(scores)}")
    print(f"最低: {min(scores):.3f}, 最高: {max(scores):.3f}, 平均: {sum(scores)/len(scores):.3f}")
    print(f"高置信(>0.9): {sum(1 for s in scores if s > 0.9)}/{len(scores)} ({100*sum(1 for s in scores if s > 0.9)/len(scores):.0f}%)")
    print(f"低置信(<0.7): {sum(1 for s in scores if s < 0.7)}/{len(scores)} ({100*sum(1 for s in scores if s < 0.7)/len(scores):.0f}%)")
    print(f"中置信:       {sum(1 for s in scores if 0.7 <= s <= 0.9)}/{len(scores)}")

out_dir = Path(r'D:\bigcreat_project\data\ocr_real_test')
out_dir.mkdir(parents=True, exist_ok=True)
result_path = out_dir / 'result.json'
with open(result_path, 'w', encoding='utf-8') as f:
    json.dump({
        'image': str(img_path),
        'perf': {'load': t_after_load - t_start, 'infer': t_end - t_after_load, 'total': t_end - t_start},
        'items': all_texts,
        'stats': {
            'total_lines': len(scores),
            'min_score': min(scores),
            'max_score': max(scores),
            'avg_score': sum(scores)/len(scores),
            'high_conf': sum(1 for s in scores if s > 0.9),
            'low_conf': sum(1 for s in scores if s < 0.7),
            'mid_conf': sum(1 for s in scores if 0.7 <= s <= 0.9),
        }
    }, f, ensure_ascii=False, indent=2)
print(f"\n结果已保存到: {result_path}")