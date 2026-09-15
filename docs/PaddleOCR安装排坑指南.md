# PaddleOCR 安装排坑指南（Windows + Python 3.12 + CPU）

> 适用：本项目 2 号成员第 1 阶段 OCR 环境部署。
> 原则：**每条命令执行完（无论成败）都截图/复制进 docs/部署日志.md。**

## 一、推荐安装路径（2026-09-01 实测可用版本组合）

> ⚠️ 已在本机（Win11 + Python 3.12.10 + CPU）完整验证，直接照抄即可。
> 最终组合：paddlepaddle 3.3.1 + paddleocr 3.7.0 + paddlex 3.7.2

```bash
# 1. 先激活项目虚拟环境
cd D:\bigcreat_project
venv\Scripts\activate

# 2. 装 CPU 版 PaddlePaddle（不锁 3.0.0！装最新版，老版和新 paddleocr 不兼容）
python -m pip install paddlepaddle -i https://www.paddlepaddle.org.cn/packages/stable/cpu/

# 3. 装 PaddleOCR
python -m pip install paddleocr -i https://pypi.tuna.tsinghua.edu.cn/simple

# 4. 验证
python -c "import paddle; print(paddle.__version__); paddle.utils.run_check()"
```

## 二、跑通第一个 Demo（注意必须关 MKLDNN）

```python
from paddleocr import PaddleOCR

# ⚠️ enable_mkldnn=False 必须加！本机 CPU 的 oneDNN 路径会报 NotImplementedError
ocr = PaddleOCR(use_textline_orientation=True, lang="ch", enable_mkldnn=False)
result = ocr.predict("test.jpg")   # 放一张试卷/题目截图
for res in result:
    print(res["rec_texts"])
```

首次运行会**自动下载模型**到 `C:\Users\<用户名>\.paddlex\official_models\`（PP-OCRv6 检测+识别模型，约几十 MB），如果下载慢/失败见下面坑 5。

## 三、高频坑速查表

| # | 症状 | 原因 | 解决 |
|---|------|------|------|
| 1 | paddlepaddle 装不上，提示找不到版本 | Python 版本不受支持 | 用 venv 里的 3.12，不要用 3.13 |
| 2 | pip install paddleocr 依赖解析冲突 | numpy/opencv 版本打架 | 先 `pip install "numpy<2.0"` 再装 paddleocr |
| 3 | 导入时报 DLL load failed | 缺 VC 运行库 | 装 Microsoft Visual C++ Redistributable 2015-2022 |
| 4 | ImportError: cannot import name 'PaddleOCR' | paddleocr 与 paddle 版本不匹配 | `pip uninstall paddleocr paddlepaddle` 后严格按上面顺序重装 |
| 5 | 首次运行卡在模型下载 | paddle 官方源慢 | 按报错里的 URL 手动下载模型放进 `~/.paddleocr/`；或多试几次 |
| 6 | 识别图片报 reshape/维度错误 | 图片路径含中文或图片损坏 | 用英文路径测试；确认图片能正常打开 |
| 7 | CPU 推理慢（>10s/张） | 正常现象 | 第 2 阶段调优：减小 max_side_len（如 960）；MKLDNN 在本机不可用 |
| 8 | `ValueError: Type of attribute: strides is not right` | paddlepaddle 3.0.0 太老，与 paddleocr 3.7 不兼容 | 升级：`pip install paddlepaddle --upgrade -i https://www.paddlepaddle.org.cn/packages/stable/cpu/`（实测装到 3.3.1 解决） |
| 9 | `NotImplementedError: ConvertPirAttribute2RuntimeAttribute not support ... onednn_instruction.cc` | CPU oneDNN 加速路径不支持部分算子属性（本机已复现） | PaddleOCR 初始化加 `enable_mkldnn=False` |
| 10 | pip 卸载依赖报 `SAFE_DELETE_FAIL_CLOSED` / 回收站相关 OSError | Windows 安全删除机制拦截了 pip 的文件删除 | 重跑一次安装；残留 `~xxx` 目录用 `python -c "import shutil; shutil.rmtree(r'路径')"` 清理后重装 |
| 11 | 大图（如 1280×3893 的整张试卷长图）识别时**无报错直接静默崩溃/进程被杀** | oneDNN 崩溃 bug 与内存问题在大图上被放大，且 `enable_mkldnn=False` 只对部分算子生效 | **切片处理**：每片 ≤1280×300，用 PIL 切成多条分别识别再合并坐标 |
| 12 | 同一进程里第 2 次调用 `predict()` 失败或行为异常 | 进程内 paddle 运行时状态被第 1 次调用污染 | **每次 OCR 单独起一个 Python 子进程**跑完即退出；生产代码用 `subprocess` 调 worker 脚本 |
| 13 | 切片识别耗时 370~880 秒/片（预期 1~3 秒） | CPU 推理 + oneDNN 被禁用 + 反复重新初始化模型 | 属**性能问题非功能问题**，第 2 阶段优化：换 PP-OCRv5_mobile 轻量模型、`text_det_limit_side_len=960`、复用常驻服务进程 |
| 14 | 手写批注（红/蓝笔）几乎识别不出来 | PP-OCRv6 通用模型以印刷体为主 | 第 2 阶段方案：换手写专用模型或走"印刷体识别 + 手写版面分析"两路 |
| 15 | 并发同时跑多个 OCR 子进程时部分切片随机失败 | CPU 内存/线程争抢，进程相互干扰 | 串行逐片跑；失败片记录下来单独重跑（本次 13 片中 04、06 两片待补） |

> 坑 8~15 均为 2026-09-01 本机实际踩坑，完整过程见 docs/部署日志.md 第 16 步。

## 四、生产可用的切片识别流水线（实测代码位置）

针对坑 11/12 的完整解法已经落地，可直接复用：

- `src/backend/ocr_run_real_exam3.py` — 切片调度器：把长图按 300px 高切片存 PNG
- `src/backend/ocr_one_slice.py` — 单片识别 worker（**每个子进程只跑一片**，必传英文路径）
- `src/backend/ocr_summarize.py` — 汇总各片 JSON → `result_real_exam.json` + `recognized_text.txt`
- 实测数据：1280×3893 数学分析试卷 → 11/13 片成功、182 行、平均置信度 0.826

## 五、和本项目的衔接点（提前知道）

1. **输出格式**：`res["rec_texts"]` 是按行识别的文本列表 → 直接喂给 `src/backend/ai_interface.py` 里的 `ocr_result_to_question()`。
2. **第 2 阶段调参方向**：试卷拍照场景重点是 `det_limit_side_len`（小字别漏检）和 `drop_score`（降低误检）。
3. **离线要求**：模型下载完后全程无网可用，符合项目"本地离线"定位。
