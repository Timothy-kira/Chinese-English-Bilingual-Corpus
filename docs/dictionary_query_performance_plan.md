# Chinese Dictionary Query Performance Plan

目标：网页输入任意中文汉字或词语后，快速返回 CC-CEDICT 释义、HSK 等级、Tatoeba 例句、OpenSubtitles 日常语句。这里先定性能架构，不先写 MVP。

## 性能目标

这些目标用于决定能不能进入实现阶段：

| 查询类型 | 目标延迟 | 说明 |
| --- | ---: | --- |
| 精确词典查询 | P95 < 20 ms | 只查 CC-CEDICT + HSK。 |
| 词典 + Tatoeba 例句 | P95 < 40 ms | 返回前 5-10 条高质量例句。 |
| 词典 + OpenSubtitles 热例句 | P95 < 80 ms | 返回前 5-10 条，不做全量 count。 |
| OpenSubtitles 全库兜底 | P95 < 200 ms | 只在热索引无结果或用户点“更多”时触发。 |
| 前端输入响应 | 不阻塞主线程 | 120-180 ms debounce，请求可取消。 |

硬约束：

- 运行时禁止 `LIKE '%词%'` 扫描普通表。
- 所有主查询必须通过 `EXPLAIN QUERY PLAN` 验证为 `SEARCH` 或 FTS index，不允许扫 1100 万字幕行。
- 所有接口默认 `LIMIT`，禁止为了展示第一页结果去 `COUNT(*)` 大表。
- 原始数据不直接参与查询；运行时只查构建好的 `dictionary.db`。

## 当前数据规模

已经下载的原始数据规模：

| 数据 | 规模 |
| --- | ---: |
| CC-CEDICT | 125,045 词条 |
| Tatoeba Mandarin | 87,264 句 |
| Tatoeba cmn-eng links | 77,181 链接 |
| Tatoeba English | 2,029,733 句 |
| HSK 3.0 expanded | 11,165 行 |
| HSK chars | 3,000 字 |
| OpenSubtitles en-zh_cn | 11,203,286 对齐句对 |

OpenSubtitles 是主压力源。它必须离线构建索引，不能运行时扫 zip 或扫文本。

## 技术选择

默认选择 SQLite 单文件数据库，原因：

- 词典和例句是读多写少，适合离线构建、运行时只读。
- 低运维成本：一个 `dictionary.db` 文件即可部署。
- SQLite FTS5 支持全文索引、trigram 子串索引、external/contentless 表、`optimize` 合并索引段。
- 对本地桌面或单机网页服务，SQLite 比 PostgreSQL 轻很多。

如果未来变成多人在线服务、高并发写入、远程部署，再切 PostgreSQL。PostgreSQL 的 `pg_trgm` 官方支持 GIN/GiST trigram 索引，可加速相似搜索和 `LIKE`/正则等查询，但运维成本更高。

## 查询模型

查询入口先把用户输入标准化：

1. trim 空白。
2. 全角/半角归一。
3. 繁简映射候选保留，不直接丢弃原形。
4. 记录长度：1 字、2 字、3 字以上走不同路由。
5. 只对中文核心查询走中文索引；英文或拼音另设独立路径。

查询结果分三层：

1. `dictionary`: CC-CEDICT + HSK，必须同步返回。
2. `examples_hot`: Tatoeba + OpenSubtitles 高质量热例句，必须同步返回。
3. `examples_deep`: OpenSubtitles 全库兜底，异步或“更多”触发。

## 数据库结构

### 词典主表

```sql
CREATE TABLE lexeme (
  lexeme_id INTEGER PRIMARY KEY,
  simplified TEXT NOT NULL,
  traditional TEXT NOT NULL,
  pinyin_numbered TEXT NOT NULL,
  pinyin_display TEXT,
  definitions_json TEXT NOT NULL,
  hsk_word_level TEXT,
  hsk_char_level TEXT,
  cedict_line TEXT NOT NULL
);
```

### 表面形索引

一个词可能用简体、繁体、变体查询到同一条词典记录。

```sql
CREATE TABLE lexeme_surface (
  surface TEXT NOT NULL,
  lexeme_id INTEGER NOT NULL,
  surface_kind INTEGER NOT NULL,
  word_len INTEGER NOT NULL,
  hsk_rank INTEGER,
  PRIMARY KEY (surface, lexeme_id)
) WITHOUT ROWID;

CREATE INDEX idx_lexeme_surface_rank
ON lexeme_surface(surface, hsk_rank, lexeme_id);
```

精确词典查询永远从 `lexeme_surface.surface = ?` 开始，再按 `lexeme_id` 回表。

### 字级 HSK

```sql
CREATE TABLE hanzi_char (
  hanzi TEXT PRIMARY KEY,
  hsk_level TEXT,
  writing_level TEXT,
  traditional TEXT,
  examples TEXT
) WITHOUT ROWID;
```

单字查询先查 `hanzi_char`，再查 `lexeme_surface`。

### 例句主表

```sql
CREATE TABLE sentence (
  sentence_id INTEGER PRIMARY KEY,
  source INTEGER NOT NULL,          -- 1=Tatoeba, 2=OpenSubtitles
  zh TEXT NOT NULL,
  en TEXT,
  zh_norm TEXT NOT NULL,
  quality_score REAL NOT NULL,
  length_chars INTEGER NOT NULL
);
```

索引：

```sql
CREATE INDEX idx_sentence_source_quality
ON sentence(source, quality_score DESC, sentence_id);
```

### 词/字倒排索引

这是性能核心。离线解析所有例句，把命中的词、字、短语写入倒排表。运行时不扫句子表。

```sql
CREATE TABLE example_hit (
  query_key TEXT NOT NULL,
  source INTEGER NOT NULL,
  sentence_id INTEGER NOT NULL,
  quality_score REAL NOT NULL,
  hit_kind INTEGER NOT NULL,         -- 1=char, 2=bigram, 3=dict_word, 4=phrase
  PRIMARY KEY (query_key, source, quality_score DESC, sentence_id)
) WITHOUT ROWID;
```

查询：

```sql
SELECT s.zh, s.en, h.quality_score
FROM example_hit h
JOIN sentence s ON s.sentence_id = h.sentence_id
WHERE h.query_key = ?
  AND h.source = ?
ORDER BY h.quality_score DESC
LIMIT ?;
```

这条路径对单字、双字、常见词都应该是毫秒级，因为它是 B-tree 前缀精确命中。

### FTS5 trigram 兜底

用于 3 个字以上、未进入 `example_hit` 的任意子串查询。

```sql
CREATE VIRTUAL TABLE sentence_fts
USING fts5(
  zh_norm,
  content='sentence',
  content_rowid='sentence_id',
  tokenize='trigram',
  detail=none,
  columnsize=0
);
```

注意：

- SQLite 官方文档说明 trigram 支持一般子串匹配，也支持 indexed `LIKE`/`GLOB`。
- 官方文档同时说明少于 3 个 Unicode 字符的全文查询不会命中，所以单字/双字不能依赖 trigram。
- `detail=none` 和 `columnsize=0` 用于降低索引体积；我们不依赖 FTS 做高亮或 BM25 排名。

兜底查询只取候选，仍然二次验证：

```sql
SELECT s.sentence_id, s.zh, s.en, s.quality_score
FROM sentence_fts f
JOIN sentence s ON s.sentence_id = f.rowid
WHERE sentence_fts MATCH ?
  AND instr(s.zh_norm, ?) > 0
ORDER BY s.quality_score DESC
LIMIT ?;
```

## 离线构建策略

1. 解析 CC-CEDICT 到 `lexeme`。
2. 解析 HSK word/char，按简体、繁体、拼音 join 到 `lexeme`。
3. 解析 Tatoeba，使用 `cmn-eng_links` 连接英文翻译。
4. 流式解析 OpenSubtitles zip，不落巨型解压文本。
5. 标准化每条中文句子，计算 `quality_score`。
6. 用词典表构建 matcher，给句子打 `example_hit`：
   - 单字命中：只对有 HSK 或常用字入索引，避免“的/了/是”爆炸。
   - 双字命中：所有字面 bigram 进入候选，但每个 key 只保留 top N。
   - 词典词命中：以 CC-CEDICT/HSK 词为主。
7. 对每个 `query_key + source` 做 top-N 截断：
   - Tatoeba: top 50。
   - OpenSubtitles hot: top 100。
   - 全量更多结果由 FTS 兜底。
8. 批量建索引，不边插边建所有索引。
9. 构建完成后运行：
   - `INSERT INTO sentence_fts(sentence_fts) VALUES('rebuild');`
   - `INSERT INTO sentence_fts(sentence_fts) VALUES('optimize');`
   - `PRAGMA optimize;`
   - `PRAGMA integrity_check;`

## 质量分计算

例句质量直接影响速度，因为我们只返回 top N。

建议评分：

```text
quality_score =
  source_weight
  + length_score
  + chinese_ratio_score
  + punctuation_score
  + translation_score
  - noise_penalty
  - too_long_penalty
  - duplicate_penalty
```

过滤规则：

- 优先 6-35 个中文字的句子。
- 丢弃乱码、HTML、过长字幕、纯标点、重复行。
- OpenSubtitles 中英文都保留，但中文展示优先。
- 同一句或高度相似句只保留最高分版本。
- 对敏感、粗俗、成人、极端内容打低分或默认不展示。

## 运行时优化

SQLite 连接：

```sql
PRAGMA query_only = ON;
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -200000; -- 约 200MB，可按机器调整
PRAGMA mmap_size = 268435456;
PRAGMA optimize;
```

如果数据库在运行时仍会更新，用 WAL：

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
```

如果数据库发布后只读，用 URI 方式打开：

```text
file:dictionary.db?mode=ro&immutable=1
```

只读 immutable 模式可以跳过锁和变更检测，但前提是文件绝对不会被修改。

应用层：

- 所有 SQL 使用 prepared statements。
- 对查询结果做 LRU cache：key = `normalized_query + options`。
- 前端 debounce 120-180 ms，输入变化取消旧请求。
- API 不返回巨量数据，一页 10 条，更多用 cursor。
- 热查询可以预热：`你`, `我`, `是`, `你好`, `谢谢`, `爱`, `学习` 等。

## 验收基准

必须建立 benchmark，而不是凭感觉。

测试 query set：

| 类型 | 示例 |
| --- | --- |
| 高频单字 | `的`, `我`, `你`, `爱` |
| 低频单字 | `龘`, `鬱` |
| 高频双字 | `你好`, `谢谢`, `中国` |
| 词典词 | `学习`, `电脑`, `数据库` |
| 长短语 | `我爱你`, `你在干什么` |
| 无结果 | `不存在的奇怪短语zz` |
| 繁体 | `學習`, `電腦` |

每条查询记录：

- total latency
- dictionary latency
- Tatoeba latency
- OpenSubtitles hot latency
- OpenSubtitles deep latency
- rows returned
- SQL query plan
- cache hit/miss

过线标准：

- 精确词典查询不得出现全表 scan。
- 热例句查询不得扫 `sentence`。
- 单字/双字不得走 FTS trigram。
- 3 字以上 fallback 必须使用 FTS index，再 `instr` 验证。
- P95 达标后才开始写网页 UI。

## 为什么不直接做 MVP

一个看起来简单的 MVP 通常会写成：

```sql
SELECT * FROM sentence WHERE zh LIKE '%' || ? || '%' LIMIT 10;
```

这在 8 万 Tatoeba 句子里看起来能跑，但到 1100 万 OpenSubtitles 行时会退化成全表扫描。更糟的是，单字查询如 `的` 会命中海量行，排序和去重都会继续放大成本。

正确路线是：运行时只查索引，复杂工作全部前移到离线构建阶段。

## 参考依据

- SQLite FTS5: https://sqlite.org/fts5.html
- SQLite query planner: https://www.sqlite.org/queryplanner.html
- SQLite EXPLAIN QUERY PLAN: https://sqlite.org/eqp.html
- SQLite ANALYZE / PRAGMA optimize: https://sqlite.org/lang_analyze.html
- SQLite WAL: https://sqlite.org/wal.html
- SQLite URI immutable: https://sqlite.org/uri.html
- PostgreSQL pg_trgm: https://www.postgresql.org/docs/current/pgtrgm.html
