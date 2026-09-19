# 大学生全场景轻量化智能学情管理与学习系统（后端部分）

> 大创项目 · 2 号成员 Mireya 负责：**Python 后端 + OCR 底层 + AI 接口联动**
> 项目基于 Qwerty Learner / Wrong-Notebook / Anki / PaddleOCR / Ollama 二次开发与深度融合

---

## 一、这个仓库里有什么

一套**能离线跑通的后端**：从试卷图片 OCR 识别，到题目切分、错题入库、AI 解析、质量门禁、
学情统计、间隔重复复习，全部通过 HTTP 接口对外提供。

```
图片 → PaddleOCR(73秒/卷) → 题目切分 → 质量门禁 → 入库
                                              ↓
                                        AI 解析(Ollama)
                                              ↓
                              自动分类 + Anki 卡片 + 学情统计
                                              ↓
                                      HTTP 接口（14 个）
```

## 二、目录结构

```
src/backend/
  api.py                 HTTP 接口层（FastAPI，14 个接口）
  api_smoke_test.py      接口冒烟测试（跑一遍验证所有接口）
  ai_interface.py        AI 唯一出口（5 个场景 + 2 个实验场景）
  ocr_fast_run.py        PaddleOCR 整卷识别（调优后 73 秒）
  ocr_ai_pipeline.py     OCR → AI 端到端联调
  ocr_split.py           题目切分 v1（纯正则）
  ocr_split_v2.py        题目切分 v2（+孤儿块回收 +选项块切分）
  eval_split.py          切分效果评估（对照人工基准）
  ocr_quality.py         OCR 质量门禁（低分题不送 AI）
  ocr_to_db.py           错题入库
  ocr_recover.py         漏题找回
  ocr_manual_import.py   人工补录导入
  error_classify.py      错题自动分类（四类错误原因）
  study_stats.py         跨线学情统计（单词线 + 错题线）
  ai_diagnose.py         AI 薄弱点诊断
  word_import.py         单词练习记录导入（支持 Qwerty Learner 导出）
  practice_seed.py       刷题/计时演示数据
  anki_sm2.py            SM-2 间隔重复算法
  anki_sync.py           错题/易错词 → Anki 卡片
  anki_export.py         卡片导出为 CSV（→ Anki 桌面端）
  anki_import_csv.py     从 CSV 导入卡片（← Anki 桌面端）
  db/
    schema.sql           数据库结构（11 + 3 张表）
    init_db.py           建库
    db.py                统一连接层（全项目唯一连库入口）
    seed.py              种子数据
    migrate_*.py          各次字段/表迁移（幂等）

docs/
  后端接口对接文档.md     接口说明（给前端队友）
  后端字段字典.md         枚举字段约定
  对接确认表.md           待队友确认事项
  二号成员进度与对接清单.md
  回应3号图表数据需求.md
  第二阶段错误日志.md      全部实测数据与踩坑（30 条）
  部署日志.md            第一阶段环境部署
  学情诊断报告.md         AI 诊断输出样例

data/ocr_fast_test/
  golden_splits.json     切分人工基准（12 题标注）
  pending_manual.json    待人工补录的题目
```

## 三、快速开始

```powershell
# 0. 进入项目根目录（所有命令都在这执行）
cd D:\bigcreat_project

# 1. 装依赖
venv\Scripts\pip.exe install fastapi uvicorn -i https://pypi.tuna.tsinghua.edu.cn/simple

# 2. 建库（幂等，可重复执行）
venv\Scripts\python.exe src\backend\db\init_db.py

# 3. 起服务
venv\Scripts\python.exe -m uvicorn src.backend.api:app --port 8000

# 4. 打开接口文档
#    http://127.0.0.1:8000/docs

# 5. 跑冒烟测试验证 14 个接口
venv\Scripts\python.exe src\backend\api_smoke_test.py
```

> AI 相关功能需要 Ollama：`D:\Ollama\ollama.exe serve`

## 四、当前进度（2026-09-19）

| 模块 | 状态 |
|---|---|
| 数据库（14 张表） | ✅ |
| OCR 调优（整卷 73 秒） | ✅ |
| 题目切分（召回 75%、精确率 100%） | ✅ |
| AI 解析链路（9/9 格式达标） | ✅ |
| OCR 质量门禁 | ✅ |
| 错题自动分类 | ✅ |
| 单词线（2607 词 + 练习记录） | ✅ |
| HTTP 接口层（14 个） | ✅ |
| Anki（SM-2 + 卡片 + CSV 双向同步） | ✅ |
| **AI 内容质量** | ⏳ 等 7B 模型（当前 0.5b 只能验证格式） |

## 五、已知限制

1. **AI 输出质量**：当前仅有 0.5b 模型，数学解析与学情诊断均为幻觉，需 7B 及以上
2. **部分统计为演示数据**：趋势图/时长图由脚本生成，需替换为前端真实上报
3. **无鉴权**：本地单机单人使用，未做登录体系
4. **未包含上游开源项目**：`upstream/` 已忽略，需单独克隆（地址见统筹文档）

## 六、实测数据（可直接用于结题材料）

| 指标 | 数值 |
|---|---|
| OCR 整卷耗时 | 73 秒（原方案 2 小时） |
| 切分召回率 / 精确率 | 75% / 100% |
| AI 单题解析 | 平均 6.9 秒，格式达标 9/9 |
| 接口响应 | < 100ms，14 个接口冒烟测试全绿 |
| 词库 | 2607 个 CET4 单词 |
| Anki 卡片 | 自动生成 63 张（13 错题 + 50 易错词） |
