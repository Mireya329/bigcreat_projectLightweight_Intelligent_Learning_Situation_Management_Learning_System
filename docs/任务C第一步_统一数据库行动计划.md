# 任务 C · 第一步行动计划：统一数据库

> 负责人：2号成员（后端）
> 前置：任务 A（OCR 调优）✅ 定版 4.26s/片；任务 B（AI 联调）✅ 全链路打通
> 目标：**一次把单词线和错题线的地基打在同一套 Schema 上**，避免后面两条线各建各的库再返工

---

## 一、现状盘点（动手前先确认）

| 项目 | 状态 | 说明 |
|------|------|------|
| `data/learning.db` | ❌ 不存在 | 本次要产出的核心文件 |
| SQLite | ✅ 3.49.1 | Python 标准库自带，**不用 pip 装** |
| 词库原料 | ✅ 380 个 JSON | `upstream/QwertyLearner/public/dicts/`，CET4_T 单库 2607 词 |
| 词库字段 | ✅ `name/trans/usphone/ukphone` | 4 个字段，trans 是数组 |
| 错题原料 | ✅ 116 行 OCR 文本 | `data/ocr_fast_test/all_text.txt` |
| 错题模型参考 | ✅ Prisma schema | `upstream/wrong-notebook/prisma/schema.prisma`，7 张表 |
| 单词记录参考 | ✅ TypeScript 接口 | `upstream/QwertyLearner/src/utils/db/record.ts`，`IWordRecord`/`IChapterRecord` |

**实测命令**（已跑通，可直接复现）：
```
D:\bigcreat_project\venv\Scripts\python.exe -c "import sqlite3; print(sqlite3.sqlite_version)"
→ 3.49.1
```

---

## 二、四个设计决策（先定死，后面不许改）

### 决策 1：单个库文件 `data/learning.db`，单词和错题共用
**理由**：学情诊断（任务 D）要跨线统计——"这个学生英语单词正确率 62%、英语错题集中在从句"，分库就得写两遍查询还得手动拼。一个库、一套 `user_id`，后面所有统计都是一句 JOIN。

### 决策 2：主键用 `INTEGER AUTOINCREMENT`，不用 cuid
**理由**：原项目用 cuid（字符串）是因为它是 TypeScript + Prisma 生态。我们后端是 Python + 原生 sqlite3，自增整数主键更简单、索引更小、JOIN 更快，手写 SQL 时也不用拼引号。**迁移到 MySQL/Postgres 时改一行类型即可**，不影响上层代码。

### 决策 3：时间统一存 `TEXT`，格式 `YYYY-MM-DD HH:MM:SS`
SQLite 没有原生日期类型。用 `datetime('now','localtime')` 默认值 + ISO 格式字符串，好处：① 直接可读，调试时不用转换；② 字符串比较即时间比较，`ORDER BY created_at` 和 `WHERE created_at > '2026-09-15'` 都能正确工作；③ Python `datetime` 可以直接接。

### 决策 4：数组/对象字段（trans、timing、mistakes）存 `TEXT`，内容为 JSON
**理由**：SQLite 没有数组类型。存 JSON 字符串是标准做法，Python 侧 `json.dumps/loads` 一行转换。**代价**：不能对这些字段内部做 SQL 查询（比如"查所有含'n. 取消'释义的词"）—— 当前阶段不需要，真需要时再加 FTS5 全文索引表。

---

## 三、表清单（10 张，一次建齐）

| # | 表名 | 归属 | 作用 | 对齐的上游模型 |
|---|------|------|------|--------------|
| 1 | `users` | 公共 | 用户 | wrong-notebook `User` |
| 2 | `subjects` | 公共 | 学科（数学/英语…） | wrong-notebook `Subject` |
| 3 | `knowledge_tags` | 公共 | 知识点标签，支持无限层级 | wrong-notebook `KnowledgeTag` |
| 4 | `error_items` | 错题 | 错题主表 | wrong-notebook `ErrorItem` |
| 5 | `error_item_tags` | 错题 | 错题↔知识点 多对多 | Prisma 隐式多对多 |
| 6 | `review_schedules` | 错题 | 艾宾浩斯复习计划 | wrong-notebook `ReviewSchedule` |
| 7 | `dicts` | 单词 | 词库元信息（380 个 JSON 各一条） | QwertyLearner `dictionary.ts` |
| 8 | `words` | 单词 | 单词明细 | QwertyLearner dict JSON |
| 9 | `word_records` | 单词 | 单次单词练习记录 | `IWordRecord` |
| 10 | `chapter_records` | 单词 | 章节练习汇总 | `IChapterRecord` |
| 11 | `ai_logs` | 公共 | AI 调用日志 | 新增（结题材料用） |

> 实际 11 张。第 11 张 `ai_logs` 是原项目没有的，**强烈建议加**：它会记录每次 AI 调用的 scene/模型/耗时/返回，任务 B 里那次"96.3 秒、四段结构完整"的实测数据就能自动沉淀，不用再手动往日志里抄。

---

## 四、完整建表 SQL（STEP 1 直接存成文件即可）

```sql
-- ============================================================
-- 大创项目统一数据库 Schema  v1.0
-- 文件：src/backend/db/schema.sql
-- 编码：UTF-8   引擎：SQLite 3.49.1
-- ============================================================

PRAGMA foreign_keys = ON;

-- ---------- 1. 用户表 ----------
CREATE TABLE IF NOT EXISTS users (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  username        TEXT    NOT NULL UNIQUE,
  password_hash   TEXT    NOT NULL,
  nickname        TEXT,
  education_stage TEXT,                        -- primary/junior_high/senior_high/university
  enrollment_year INTEGER,
  role            TEXT    NOT NULL DEFAULT 'user',
  is_active       INTEGER NOT NULL DEFAULT 1,
  created_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at      TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ---------- 2. 学科表（单词/错题共用） ----------
CREATE TABLE IF NOT EXISTS subjects (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  name       TEXT    NOT NULL,
  created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(user_id, name)
);

-- ---------- 3. 知识点标签（支持无限层级，邻接表模式） ----------
CREATE TABLE IF NOT EXISTS knowledge_tags (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  name       TEXT    NOT NULL,
  subject    TEXT,
  parent_id  INTEGER REFERENCES knowledge_tags(id) ON DELETE SET NULL,
  sort_order INTEGER NOT NULL DEFAULT 0,
  code       TEXT,                             -- 如 "1.2.1"
  is_system  INTEGER NOT NULL DEFAULT 0,       -- 1=系统预设 0=用户自定义
  user_id    INTEGER REFERENCES users(id) ON DELETE CASCADE,
  created_at TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  UNIQUE(subject, name, user_id, parent_id)
);
CREATE INDEX IF NOT EXISTS idx_tags_parent  ON knowledge_tags(parent_id);
CREATE INDEX IF NOT EXISTS idx_tags_subject ON knowledge_tags(subject);

-- ---------- 4. 错题主表 ----------
CREATE TABLE IF NOT EXISTS error_items (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  subject_id         INTEGER REFERENCES subjects(id) ON DELETE SET NULL,
  original_image_url TEXT,                     -- 原图（本地路径）
  ocr_text           TEXT,                     -- OCR 原始全文
  question_text      TEXT,                     -- 切分后的题目
  answer_text        TEXT,                     -- 正确答案
  analysis           TEXT,                     -- AI 解析
  wrong_answer_text  TEXT,                     -- 学生错答
  mistake_analysis   TEXT,                     -- 错因分析
  mistake_status     TEXT,                     -- not_attempted/wrong_attempt/unknown
  error_type         TEXT,                     -- Calculation/Concept/Misread/MethodMissing
  source             TEXT,                     -- 来源，如"期中考试"
  user_notes         TEXT,
  mastery_level      INTEGER NOT NULL DEFAULT 0,  -- 0未掌握 1复习中 2已掌握
  grade_semester     TEXT,
  paper_level        TEXT,
  ai_model           TEXT,                     -- 生成解析的模型名，便于对比
  ai_elapsed_ms      INTEGER,
  created_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime')),
  updated_at         TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_err_user    ON error_items(user_id);
CREATE INDEX IF NOT EXISTS idx_err_subject ON error_items(subject_id);
CREATE INDEX IF NOT EXISTS idx_err_mastery ON error_items(mastery_level);

-- ---------- 5. 错题↔知识点 多对多 ----------
CREATE TABLE IF NOT EXISTS error_item_tags (
  error_item_id INTEGER NOT NULL REFERENCES error_items(id) ON DELETE CASCADE,
  tag_id        INTEGER NOT NULL REFERENCES knowledge_tags(id) ON DELETE CASCADE,
  PRIMARY KEY (error_item_id, tag_id)
);

-- ---------- 6. 复习计划（艾宾浩斯） ----------
CREATE TABLE IF NOT EXISTS review_schedules (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  error_item_id INTEGER NOT NULL REFERENCES error_items(id) ON DELETE CASCADE,
  scheduled_for TEXT    NOT NULL,
  completed_at  TEXT,
  is_correct    INTEGER,                       -- NULL未做 0错 1对
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_rev_sched ON review_schedules(scheduled_for);

-- ---------- 7. 词库元信息 ----------
CREATE TABLE IF NOT EXISTS dicts (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  dict_key    TEXT    NOT NULL UNIQUE,         -- 如 CET4_T
  name        TEXT    NOT NULL,
  description TEXT,
  language    TEXT    NOT NULL DEFAULT 'en',
  category    TEXT,                            -- 四级/六级/考研/专业
  word_count  INTEGER NOT NULL DEFAULT 0,
  source_file TEXT,
  created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ---------- 8. 单词明细 ----------
CREATE TABLE IF NOT EXISTS words (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  dict_id     INTEGER NOT NULL REFERENCES dicts(id) ON DELETE CASCADE,
  word        TEXT    NOT NULL,
  trans       TEXT,                            -- JSON 数组
  usphone     TEXT,
  ukphone     TEXT,
  chapter     INTEGER NOT NULL DEFAULT 0,
  order_index INTEGER NOT NULL DEFAULT 0,      -- 词库内原始顺序
  created_at  TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_words_dict ON words(dict_id);
CREATE INDEX IF NOT EXISTS idx_words_word ON words(word);

-- ---------- 9. 单词练习记录 ----------
CREATE TABLE IF NOT EXISTS word_records (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  word_id       INTEGER REFERENCES words(id) ON DELETE SET NULL,
  dict_key      TEXT    NOT NULL,
  chapter       INTEGER,
  timing        TEXT,                          -- JSON 数组，每字母间隔 ms
  wrong_count   INTEGER NOT NULL DEFAULT 0,
  mistakes      TEXT,                          -- JSON 对象 {字母索引: [错按的键]}
  total_time_ms INTEGER,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_wrec_user ON word_records(user_id, created_at);

-- ---------- 10. 章节练习汇总 ----------
CREATE TABLE IF NOT EXISTS chapter_records (
  id                   INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  dict_key             TEXT    NOT NULL,
  chapter              INTEGER,
  duration_sec         INTEGER,
  correct_count        INTEGER NOT NULL DEFAULT 0,
  wrong_count          INTEGER NOT NULL DEFAULT 0,
  word_count           INTEGER NOT NULL DEFAULT 0,
  correct_word_indexes TEXT,                   -- JSON 数组
  word_number          INTEGER,
  created_at           TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);

-- ---------- 11. AI 调用日志 ----------
CREATE TABLE IF NOT EXISTS ai_logs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
  scene         TEXT    NOT NULL,              -- explain_wrong/qa/diagnose/...
  model         TEXT,
  error_item_id INTEGER REFERENCES error_items(id) ON DELETE SET NULL,
  prompt_chars  INTEGER,
  response      TEXT,
  elapsed_ms    INTEGER,
  ok            INTEGER NOT NULL DEFAULT 1,
  error_msg     TEXT,
  created_at    TEXT    NOT NULL DEFAULT (datetime('now','localtime'))
);
CREATE INDEX IF NOT EXISTS idx_ailogs_scene ON ai_logs(scene, created_at);
```

---

## 五、执行步骤（今晚 2 小时档）

### STEP 1 — 建目录 + 存 schema.sql（10 分钟）
- 新建目录 `src/backend/db/`
- 把上面 SQL 存成 `src/backend/db/schema.sql`
- **验收**：文件存在，UTF-8 编码

### STEP 2 — 写 `init_db.py` 建库（20 分钟）
- 新建 `src/backend/db/init_db.py`
- 逻辑：`sqlite3.connect('data/learning.db')` → 读 schema.sql → `executescript()` → 查 `sqlite_master` 列出所有表并打印
- 关键：连接后**第一句必须是 `PRAGMA foreign_keys = ON`**，否则外键约束不生效（SQLite 默认关闭，这是最经典的坑）
- **验收**：运行后打印出 11 张表名，`data/learning.db` 文件生成

### STEP 3 — 写 `db.py` 统一连接层（15 分钟）
- 新建 `src/backend/db/db.py`，提供：
  - `get_conn()`：返回已开外键、`row_factory = sqlite3.Row` 的连接（Row 让你可以用 `row['word']` 而不是 `row[1]`）
  - `DB_PATH` 常量指向 `data/learning.db`
- **纪律**：从今往后所有模块**只准从这里拿连接**，谁也不许自己 `sqlite3.connect`，否则路径和 PRAGMA 会各写各的
- **验收**：`python -c "from db import get_conn; print(get_conn().execute('select 1').fetchone())"`

### STEP 4 — 写 `seed.py` 塞种子数据（25 分钟）
- 新建 `src/backend/db/seed.py`，插入：
  - 1 个测试用户（username=`test`，密码先明文占位，后面再上 bcrypt）
  - 2 个学科：`数学`、`英语`
  - 1 个知识点：`偏导数与高阶偏导数`（这就是 OCR 那张卷子的主题）
  - 1 条错题：`ocr_text` 直接塞 `data/ocr_fast_test/all_text.txt` 的全文，`subject_id` 指向数学
- **验收**：`SELECT * FROM error_items` 能查回那 1 条，`JOIN subjects` 能带出"数学"

### STEP 5 — 写 `test_crud.py` 冒烟测试（20 分钟）
- 新建 `src/backend/db/test_crud.py`
- 跑完整流程：插一条错题 → 查出来 → 改 `mastery_level` → 打标签 → 建复习计划 → 删掉
- **验收**：每步 print 出来，最后数据库回到测试前状态

### STEP 6 — 词库导入器（30 分钟，时间富裕就做）
- 新建 `src/backend/db/import_dict.py`
- 读 `upstream/QwertyLearner/public/dicts/CET4_T.json` → 写 1 条 `dicts` + 2607 条 `words`
- 用 `executemany()` 批量插，**不要 for 循环单条 insert**（2607 条差距很明显）
- **验收**：`SELECT COUNT(*) FROM words WHERE dict_id=1` → 2607

### STEP 7 — 收尾（10 分钟）
- 把今晚每条命令和结果追加进 `docs/第二阶段错误日志.md`（条目 14 起）
- `git add src/backend/db docs && git commit -m "任务C STEP1-6：统一数据库 Schema + 建库 + 词库导入"`

---

## 六、做完这一步，你手上有什么

| 产出 | 用途 |
|------|------|
| `data/learning.db` | 后端唯一数据源，后续所有模块都往这里写 |
| `src/backend/db/schema.sql` | 建表脚本，换机器一条命令重建，结题材料附录 |
| `src/backend/db/db.py` | 统一连接层，消灭"各写各的连接" |
| `src/backend/db/seed.py` | 演示数据，给前端联调用 |
| `src/backend/db/test_crud.py` | 回归测试，改 Schema 后跑一遍就知道有没有改坏 |
| `src/backend/db/import_dict.py` | 380 个词库随时可灌，单词线的数据基础 |

---

## 七、下一步（明天起的排期建议）

1. **错题链路**：`ocr_to_db.py` —— 题目切分（按题号正则 `^\d+[.．]` 和 `一、二、` 分节）→ 结构化入库 → 调 `explain_wrong_question()` → 结果写回 `analysis` 字段 + 记 `ai_logs`
2. **单词链路**：QwertyLearner 前端练习数据 → 后端 `word_records` 落库（改前端 IndexedDB 写入逻辑，或先做手动导入）
3. **知识联动**：错题里的英文题干 → 提取生词 → 自动进单词本（这是两个系统真正的融合点，也是你项目最能吹的亮点）

---

## 八、风险与待确认

| 风险 | 影响 | 处理 |
|------|------|------|
| 密码明文存 | 安全隐患 | 当前阶段先跑通，联调前上 `bcrypt`；原项目用的就是 `bcryptjs` |
| 词库 380 个全灌约 50 万条 | 库会到几百 MB | 先只灌用到的（CET4/六级/考研），需要时再导 |
| 错题切分正则不准 | 题目切错 | 先在 `all_text.txt` 上试正则，人工核对 116 行的切分结果再写进代码 |
| Schema 后面要加字段 | 改表 | SQLite 支持 `ALTER TABLE ADD COLUMN`，加字段不用重建，别怕 |

**需要跟组长确认的一件事**：`ai_logs` 表是否要记录完整 AI 返回原文（隐私/存储权衡）。当前设计是**记全文**，因为结题材料要引用真实输出做证据。
