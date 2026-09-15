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

-- ---------- 5. 错题<->知识点 多对多 ----------
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
