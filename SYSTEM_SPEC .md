# System Specification

**Project**: Agentic Cinema — ClickHouse Track
**Version**: 1.0（基於執行計畫 v3）
**Date**: 2026-08-20

---

## 1. Scope

### 1.1 目標

一個三層 agent 系統：對 **1,238 部**可驗證歷史電影（1990–2014、具備完整 USD 票房與預算、匹配 CMU 劇情摘要）的抽象結構母題做即時多維聚合，產出有歷史類比證據（historical-analogue evidence）支撐的新題材提案，並生成可用於 pitch 的動態分鏡。

### 1.2 Non-goals（明確不做）

- 宣稱覆蓋不可驗證的四萬部語料全集（聚焦於 1,238 部具備完整財務與母題數據的高信度子集，避免 credibility gap）
- 宣稱具備上映期／開片反應的注意力資料（資料上不存在，見 §3.1 與 `docs/M1_DATA_FINDINGS.md`）
- 做出未經校準的「黑箱票房預測」（定位為歷史類比證據與指標評分：Analogue / Evidence Scoring）
- 向量檢索、混合查詢、HNSW 索引
- Veo 影片生成
- Context Caching
- 後端影片合成（moviepy / FFmpeg）
- 使用者帳號、多租戶、持久化 session
- 超過 3 個場景的分鏡
- 任何非 Google 的 AI 服務

### 1.3 硬性約束（違反即失格）

| 約束 | 驗證方式 |
|---|---|
| ClickHouse 必須於 runtime 經 `mcp-clickhouse` 存取 | 程式碼中可見 MCPToolset 掛載與 tool call |
| AI 相關服務僅限 Google Cloud | 依賴清單、Dockerfile、commit 歷史全部乾淨 |
| Repo 公開 ＋ OSI 授權於 About 區可見 | GitHub 頁面目視 |
| Hosted URL 公開可存取 | 無痕視窗測試 |

---

## 2. Architecture

```
┌──────────────────────────────────────────────────┐
│  web/index.html  （原生 JS，無建置流程）           │
│  ├─ 證據流（SSE）                                 │
│  ├─ 雙方案對比 ＋ 核准閘門                         │
│  └─ CSS Ken Burns 播放器                          │
└───────────────────┬──────────────────────────────┘
                    │ HTTP / SSE
┌───────────────────▼──────────────────────────────┐
│  Cloud Run（單一容器）                            │
│  ┌────────────────────────────────────────────┐  │
│  │ FastAPI  app/main.py                       │  │
│  │  ├─ POST /run          啟動流程            │  │
│  │  ├─ GET  /events/{id}  SSE 事件流          │  │
│  │  ├─ POST /approve/{id} 核准閘門            │  │
│  │  └─ GET  /            靜態前端             │  │
│  ├────────────────────────────────────────────┤  │
│  │ ADK Root Agent (SequentialAgent)           │  │
│  │  ├─ RecombineAgent                  ─┐     │  │
│  │  ├─ PredictAgent (Analogue Scoring) ─┼─ MCPToolset
│  │  └─ StoryboardAgent                  │  （核准後才執行）
│  ├──────────────────────────────────────┼─────┤  │
│  │ mcp-clickhouse（stdio 子行程，同容器）      │  │
│  └──────────────────────────────────────┼─────┘  │
└─────────────────────────────────────────┼────────┘
          ┌───────────────┴────────┬──────┴───────┐
          ▼                        ▼              ▼
   ClickHouse Cloud        Gemini / Imagen /    GCS
   （films + attention）   Cloud TTS            （媒體資產）
```

**離線 ETL** 不在此容器內，由本機執行後寫入 ClickHouse（見 §4）。

---

## 3. Data Model

### 3.1 `films`

> **實作檔為 `sql/001_films.sql`，那份才是 source of truth。**
> `app/prompts.py` 啟動時讀該檔組進 system instruction，本節為說明用摘要，
> 兩者不一致時以 `sql/` 為準。

```sql
CREATE TABLE films
(
    film_id              String,              -- Wikidata QID，如 'Q25188'
    enwiki_title         String,              -- 維基條目名，**含消歧義後綴**
                                              -- （47% 與 title 不同）。pageviews
                                              -- API 只認這個。
    title                String,              -- 無後綴標題，CMU join 用這個
    release_year         UInt16,
    release_bucket       LowCardinality(String), -- '1990-1994' … '2010-2014'
    genres               Array(LowCardinality(String)),
    budget_usd           Nullable(UInt64),     -- 僅 USD，其餘幣別已剔除
    revenue_usd          Nullable(UInt64),     -- 全球票房，僅 USD
    roi                  Nullable(Float32) MATERIALIZED
                            if(budget_usd > 0, revenue_usd / budget_usd, NULL),

    -- ETL 階段由 Gemini 產生（見 §4.4），值域受 `etl/vocab.py` 限制
    motif_tags           Array(LowCardinality(String)),
    act_structure        LowCardinality(String),
    character_archetypes Array(LowCardinality(String)),
    tone_axis            Float32,             -- -1 冷峻 ↔ +1 溫暖
    conflict_scale       LowCardinality(String), -- personal / communal / existential

    -- Wikipedia 關注度代理（由 film_attention 派生）
    -- 量測窗 2015-07 起，全部影片皆在該窗之前上映（延遲 1–25 年，中位數 12）。
    -- 這裡量到的是長尾查閱度，**不是上映反應**，窗內沒有首映峰值。
    interest_median_daily Nullable(UInt32),   -- 窗內每日瀏覽中位數（穩健基線）
    interest_p95_daily    Nullable(UInt32),   -- 尖峰量級
    interest_trend_slope  Nullable(Float32),  -- 窗內趨勢，>0 為上升
    interest_cohort_pct   Nullable(Float32),  -- 同 cohort 內百分位 0–1，
                                              -- attention_score 的唯一輸入
    has_interest_signal   UInt8 MATERIALIZED  -- 是否高於量測下限。71 部低於此，
                              interest_median_daily >= 50,  -- 其百分位是雜訊排名
    years_to_measurement  UInt8,              -- release_year → 2015，1–25
    attention_kind        LowCardinality(String) DEFAULT 'sustained_interest',

    ingested_at          DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (release_bucket, film_id);
```

**已移除 `pageview_peak` 與 `pageview_decay_days`。** 沒有上映峰值，
decay 無從定義；保留這兩個名字會讓 agent 依 DDL 推論出上映反應敘事。
詳見 `docs/M1_DATA_FINDINGS.md` §1。

### 3.2 `film_attention`

```sql
CREATE TABLE film_attention
(
    film_id String,
    date    Date,
    views   UInt32
)
ENGINE = MergeTree
ORDER BY (film_id, date);
```

實測 **4,937,204 列**（1,238 部 × 2015-07 至今最多 4,074 天，1,238/1,238 皆有資料）。

### 3.3 Materialized Views

三個具名 view。**agent 只查這三個 view 與 `films`，不查 `film_attention` 原始表**——把 SQL 難度前移到 schema 設計，是 LLM 產出可執行查詢的關鍵。

> **實作檔為 `sql/003_materialized_views.sql`。** 以下為說明摘要，
> 每個 view 的預期查詢形狀與典型樣本數寫在該檔註解中。

**分組粒度是這裡最重要的決定。** 1,238 部片的資料集，切太細會讓
`sample_count` 普遍低於 `MIN_SAMPLE_SIZE`（8），demo 上台時每個查詢都回
「證據不足」。

```sql
-- 角色原型表現。分組用 release_bucket 而非 release_year：
-- 25 原型 × 25 年 = 625 格，約 3,714 個 (film, archetype) 配對 → 每格 5.9 筆，低於門檻。
-- 25 原型 × 5 桶 = 125 格 → 每格 29.7 筆。
CREATE MATERIALIZED VIEW mv_archetype_performance
ENGINE = AggregatingMergeTree
ORDER BY (archetype, release_bucket)
AS SELECT
    arrayJoin(character_archetypes) AS archetype,
    release_bucket,
    countState()                            AS sample_count,
    quantileState(0.5)(roi)                 AS roi_median,
    quantileState(0.75)(roi)                AS roi_p75,
    avgState(tone_axis)                     AS avg_tone,
    -- interest 只聚合高於量測下限的片，故自帶樣本數，且必然小於 sample_count
    countIfState(has_interest_signal)       AS interest_sample_count,
    quantileStateIf(0.5)(interest_cohort_pct, has_interest_signal)
                                            AS interest_pct_median
FROM films
WHERE roi IS NOT NULL
GROUP BY archetype, release_bucket;

-- 母題兩兩組合。原本的雙 arrayJoin 寫法必須改。
CREATE MATERIALIZED VIEW mv_motif_pair_stats
ENGINE = AggregatingMergeTree
ORDER BY (motif_a, motif_b)
AS SELECT
    pair.1 AS motif_a,
    pair.2 AS motif_b,
    countState()                            AS sample_count,
    quantileState(0.5)(roi)                 AS roi_median,
    countIfState(has_interest_signal)       AS interest_sample_count,
    quantileStateIf(0.5)(interest_cohort_pct, has_interest_signal)
                                            AS interest_pct_median
FROM (
    SELECT roi, interest_cohort_pct, has_interest_signal,
        arrayJoin(arrayFilter(p -> p.1 < p.2,
            arrayFlatten(arrayMap(a -> arrayMap(b -> (a, b), motif_tags),
                                  motif_tags)))) AS pair
    FROM films WHERE roi IS NOT NULL
)
GROUP BY motif_a, motif_b;

-- 每片的關注度軌跡，按日曆年。取代 mv_attention_curve。
CREATE MATERIALIZED VIEW mv_interest_by_year
ENGINE = AggregatingMergeTree
ORDER BY (film_id, calendar_year)
AS SELECT
    film_id,
    toYear(date)    AS calendar_year,
    sumState(views) AS total_views,
    avgState(views) AS avg_daily_views
FROM film_attention
GROUP BY film_id, calendar_year;
```

**`mv_attention_curve` 已刪除。** 它以 `days_since_peak` 排序，但本資料集沒有
首映峰值可作為原點——窗內出現的最大值通常是無關的新聞事件。改用日曆年，
回答「2015 至今這部片的查閱度如何變化」，那才是資料支持得起的問題。

> 註：原本擔心雙重 `arrayJoin` 產生笛卡兒積。**實測（ClickHouse 26.2）結果更糟**：
> 同一陣列上的兩個 `arrayJoin` 會以同一索引對齊（zip），
> `['revenge','redemption','survival']` 只產生 3 列自我配對
> `(revenge, revenge)`、`(redemption, redemption)`、`(survival, survival)`，
> 不是 9 列。接著的 `WHERE motif_a < motif_b` 把它們全部濾掉，
> **view 會靜默地是空的，不會報任何錯**。
> 已改為單次 `arrayJoin` 搭配 `arrayFlatten(arrayMap(...))` 產生 pair 陣列，
> 實測 3 個母題正確產出 C(3,2)=3 個配對。

### 3.4 供 LLM 使用的 schema 說明

System instruction 中必須包含：完整 DDL、每個 view 的用途一句話、以及 **3 個範例查詢**。範例查詢至少涵蓋：陣列欄位過濾（`has(motif_tags, 'x')`）、AggregatingMergeTree 的 `-Merge` 讀取語法、樣本數過濾。

---

## 4. ETL Pipeline

五個獨立可重跑的腳本，每個輸出 parquet 到本機再載入。

### 4.1 `01_wikidata_spine.py`

Wikidata 作為主幹（提供 QID、票房、預算、類型、以及 enwiki 條目標題）。

- Endpoint: `https://query.wikidata.org/sparql`
- 條件：`?f wdt:P31/wdt:P279* wd:Q11424`（電影）、有 `wdt:P2142`（票房）、`wdt:P577` 於 2000 年後
- 取 enwiki sitelink：`?article schema:about ?f ; schema:isPartOf <https://en.wikipedia.org/>`
- **必須依年份分批查詢**，單次全量查詢會 timeout
- 授權：CC0

**輸出**：`films_spine.parquet`。原估 5000–15000 列偏高：實測 1990–2014 僅要求票房為 2,157 部，
加上預算條件為 **1,595 部**。

### 4.2 `02_cmu_join.py`

CMU Movie Summary Corpus 提供劇情文本。

- 來源：CMU Movie Summary Corpus（維基百科劇情摘要衍生，授權為 **CC BY-SA 3.0**）
- 下載 `MovieSummaries.tar.gz`，使用 `plot_summaries.txt` 與 `movie.metadata.tsv`
- **Join 風險**：CMU 用 Wikipedia page ID 與 Freebase ID，與 Wikidata QID 無直接對應。實務作法為正規化標題（小寫、去標點、去 "The"）＋ 上映年份 ±1 的模糊比對
- 預期匹配率 70–85%
- **Gate（已依實測調整）**：原訂 ≥ 1500 部。年份下界已放寬至 1990（規格允許的降級路徑），
  實得 **1,238 部**，為本資料集的實際上限——CMU 語料在 2012 年結束，再放寬年份也補不回來

**輸出**：`films_with_plots.parquet`

### 4.3 `03_pageviews.py`

- Endpoint: `https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/{title}/daily/{start}/{end}`
- **每個條目一次呼叫即回傳完整日期區間**，故 1,238 部片僅需約 1,238 次呼叫
- 條目名用 `enwiki_title`（含消歧義後綴），**不是 `title`**。用錯會全部 404
- 起始日期 20150701（API 資料起點），終止為今日（約 4,071 天）
- 加入 rate limit（每秒 ≤ 5 次）與失敗重試；404 視為正常結果不中斷
- 授權：CC0 / Wikimedia API 使用條款

**指標定義**：全部影片皆在量測窗之前上映（延遲 1–25 年，中位數 12），
故窗內**沒有**上映峰值，也沒有可定義的 decay 原點。寫回 films 的是：

| 欄位 | 意義 |
|---|---|
| `interest_median_daily` | 窗內每日瀏覽中位數。用中位數而非平均，因為單一新聞事件（演員過世、重拍宣布）會造成數量級的尖峰 |
| `interest_p95_daily` | 尖峰量級 |
| `interest_trend_slope` | 窗內趨勢，以中位數正規化後的年變化率。**條目改名會截斷窗口（56 部，4.5%），該欄位對此敏感**——見 `docs/M1_DATA_FINDINGS.md` §6.2 |
| `interest_cohort_pct` | 同 5 年 cohort 內百分位。有界 0–1，是 `attention_score` 唯一的輸入 |
| `years_to_measurement` | 上映到量測起點的年數，1–25 |

`interest_cohort_pct` 必須在全部影片抓完後統一計算——百分位需要整個 cohort 的分布。

> **為什麼要正規化（理由已修正）**：原本寫的是「量測延遲是混淆因子」，依據是
> 25 部 smoke test（原始 r = +0.272）。全量 1,238 部實測 **r = −0.009**，
> 各 cohort 關注度中位數 637/755/843/646/883 非單調、全距僅 1.4 倍，而 cohort
> **內部**跨 13–40 倍。延遲不是混淆因子；那 25 部是 `head()` 的非隨機切片。
>
> 保留此欄位的理由改為**尺度正規化**：原始日均值在單一 cohort 內就跨 13–40 倍，
> 要映射到 0–100 分必須任意選定縮放常數，百分位則天然有界。
> 完整量測見 `docs/M1_DATA_FINDINGS.md` §1。

> **量測下限（已實作）**：71 部片的原始日均中位數 < 50，其中 9 部 < 5。這些片算出的
> `interest_cohort_pct`（如 0.003）看似精確，底下只有雜訊。
>
> `sql/001` 以 `has_interest_signal UInt8 MATERIALIZED interest_median_daily >= 50`
> 標記（門檻須等於 `app/config.MIN_INTEREST_SIGNAL`，由測試斷言），
> `sql/003` 的 interest 聚合改用 `quantileStateIf` ＋ `countIfState`。
> 原始欄位保留全部影片——資料沒有錯，只是低於解析度。
>
> 因此兩個 MV 各多一個 **`interest_sample_count`**，與 `sample_count`（描述 ROI）
> 不同且較小。報告 interest 數字時必須用前者。

**輸出**：`attention.parquet`（約 504 萬列）、`films_enriched.parquet`

### 4.4 `04_motif_enrichment.py`

對每部片的劇情摘要跑一次 Gemini Flash，產出結構化母題。

```python
class FilmMotifs(BaseModel):
    motif_tags: list[str]            # 3–6 個，來自受控詞彙表
    act_structure: str               # 受控枚舉
    character_archetypes: list[str]  # 2–4 個，受控詞彙表
    tone_axis: float                 # -1.0 ~ 1.0
    conflict_scale: str              # personal | communal | existential
```

**關鍵設計**：`motif_tags` 與 `character_archetypes` 必須使用**受控詞彙表**（各 25–40 個預定義值，寫在 prompt 中），否則會產生數千個近義標籤，聚合完全失效。

- 使用 `response_mime_type="application/json"` ＋ `response_schema`
- 併發 10，含指數退避重試
- **只輸出抽象母題，不輸出劇情原文、對白或情節序列**
- 成本估算：1,238 次 Flash 呼叫 ≈ $1–3

**輸出**：`motifs.parquet`

### 4.5 `05_load_clickhouse.py`

- 使用 `clickhouse-connect`（原生 driver，僅用於寫入，不涉及 runtime）
- 建表 → 載入 → 建 MV → 驗證
- 冪等：以 `TRUNCATE` 後重載，而非 upsert

### 4.6 資料治理與 Attribution 說明

> **重要免責宣告**：以下為本專案之工程架構與資料治理分析，**非正式法律意見**。提交前請再次核對各資料源之條款與授權要求。

1. **資料源授權矩陣**：
   - **Wikidata SPARQL**：CC0。
   - **Wikimedia Pageviews API**：CC0 / 遵循 Wikimedia API 規範。
   - **CMU Movie Summary Corpus**：CC BY-SA 3.0（維基百科衍生），需清楚註明原作者與來源引用（Attribution）。
2. **資料治理原則（Data Governance）**：
   - **劇情原文不進入 repo 也不進入 ClickHouse。** CMU corpus 僅於本地 ETL 階段短暫下載處理，經 Gemini Flash 抽取高層次抽象結構特徵（母題、角色原型、結構類型）後即丟棄原始文本。
   - 資料庫與應用程式僅儲存與聚合衍生特徵，不儲存、不展示任何受版權保護的劇情原文。
   - README 與文件清楚列明所有資料來源、授權方式與特徵抽取邊界。

---

## 5. Agent Layer

### 5.1 MCP 整合

`mcp-clickhouse` 以 stdio 子行程在同容器內啟動，由 ADK 的 MCPToolset 掛載。

```python
# app/mcp.py — 介面以實作時的 ADK 版本為準
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import StdioConnectionParams
from mcp import StdioServerParameters

clickhouse_tools = MCPToolset(
    connection_params=StdioConnectionParams(
      server_params=StdioServerParameters(
        command="uv",
        args=["run", "--with", "mcp-clickhouse", "--python", "3.13", "mcp-clickhouse"],
        env={
            "CLICKHOUSE_HOST":     os.environ["CLICKHOUSE_HOST"],
            "CLICKHOUSE_PORT":     os.environ.get("CLICKHOUSE_PORT", "8443"),
            "CLICKHOUSE_USER":     os.environ["CLICKHOUSE_USER"],
            "CLICKHOUSE_PASSWORD": os.environ["CLICKHOUSE_PASSWORD"],
            "CLICKHOUSE_SECURE":   "true",
            "CLICKHOUSE_DATABASE": os.environ.get("CLICKHOUSE_DATABASE", "default"),
        },
      ),
    )
)
```

暴露的工具：`list_databases`、`list_tables`、`run_query`（預設唯讀）。

> 註：`mcp-clickhouse` 0.3.0 起工具名為 `run_query`，非 `run_select_query`（後者僅存在於 0.1.x）。唯讀由 `CLICKHOUSE_ALLOW_WRITE_ACCESS` 預設關閉保證。

**Cloud Run 注意事項**：容器映像需預裝 `uv` 與 `mcp-clickhouse`，避免冷啟動時才下載。建議在 Dockerfile 中 `uv pip install mcp-clickhouse` 後改用直接命令啟動。

### 5.2 兩段式呼叫模式

Function calling 與 `response_schema` 通常無法在同一次呼叫同時生效。所有需要「查資料再產結構化輸出」的 agent 皆採兩段：

```
Phase A（ReAct）：tools=[clickhouse_tools]，無 response_schema
  → Gemini 自主決定查詢，可多輪
  → 每次 tool call 與 result 皆推送至 SSE
Phase B（收斂）：無 tools，response_schema=<Pydantic>
  → 以 Phase A 的查詢結果為 context，產出保證結構的物件
```

### 5.3 SQL 錯誤重試

`run_select_query` 回傳錯誤時，將錯誤原文原樣回饋給 Gemini，允許最多 **2 次**自我修正。第 3 次失敗則以 `insufficient_evidence` 終止該 agent。

重試過程需完整推送至 SSE——這是「agent 而非 script」最直接的證據。

### 5.4 Agent 定義

| Agent | 輸入 | 工具 | 輸出契約 | 終止條件 |
|---|---|---|---|---|
| **RecombineAgent** | 使用者的方向提示（可空） | clickhouse_tools | `TreatmentProposal × 2` | 產出穩健＋彩蛋兩案 |
| **PredictAgent**<br>*(Analogue / Evidence Scoring)* | 兩個 proposal 的母題與原型 | clickhouse_tools | `PredictionScore × 2` | 樣本數 < 8 → `insufficient_evidence` |
| **StoryboardAgent** | 經核准的單一 proposal | Imagen、TTS、GCS | `SceneAsset × 3` | 3 組圖＋音訊 URL |

> **對外敘事與定位**：`PredictAgent` 定位為 **Analogue / Evidence Scoring Agent**，其職責並非做出無依據的「票房預測」，而是從 ClickHouse 歷史資料中檢索最具代表性的相似類比案例（historical analogues），輸出由具體查詢結果支撐的類比證據（historical-analogue evidence）與指標評分。

**RecombineAgent 的彩蛋分支**：主線完成後，額外一次 `temperature=1.5` 的呼叫，指示其產出刻意違背資料建議的組合。這是**一次呼叫**，不是第二條完整管線。

**StoryboardAgent 只在使用者核准後執行。** 這既是產品成熟度的展現，也是 credits 成本控制——開發期間的每次測試都會省下三張 Imagen 生圖費用。

### 5.5 資料契約

```python
class EvidenceItem(BaseModel):
    claim: str            # 「同類作品 ROI 中位數 2.4」
    sql_query: str        # 產生此數字的查詢
    sample_count: int
    source_view: str

class TreatmentProposal(BaseModel):
    variant: Literal["grounded", "wildcard"]
    title: str
    logline: str                      # ≤ 40 字
    motif_tags: list[str]
    character_archetypes: list[str]
    act_structure: str
    rationale: str                    # 為何選這個組合
    evidence: list[EvidenceItem]

class PredictionScore(BaseModel):
    """Analogue / Evidence Scoring 結構體：輸出歷史類比證據而非黑箱預測"""
    proposal_title: str
    commercial_score: float | None    # 0–100（同類歷史作品 ROI 分布）；None = N/A
    attention_score: float | None     # 0–100（同類作品維基關注度）；None = N/A
    composite: float | None           # 只對存在的維度加權並重新正規化
    confidence: Literal["high", "medium", "low", "insufficient_evidence"]
    evidence: list[EvidenceItem]      # 支撐評分的歷史類比查詢證據清單
    caveats: list[str]

class SceneAsset(BaseModel):
    scene_index: int
    description: str
    image_url: str                    # GCS
    audio_url: str                    # GCS
    duration_sec: float
```

**評分必須可解釋**：`composite` 不是黑箱數字，而是由 `evidence` 列表中的具體歷史類比查詢結果組成。前端要把兩者並列顯示。

**`None` 與 `0.0` 是兩件不同的事，不可互相折疊。** `None` 是「找不到可比對的東西」，
`0.0` 是「可比對的作品表現和資料集裡最差的一樣」。舊版在沒有 interest evidence 時
把 `attention_score` 設成 `0.0` 再乘權重 0.4 代入 composite，caveat 寫著
「is 0, not low」而算式仍在扣 40 分——缺資料被靜默當成扣分。

現行 `app/scoring.compute_composite()` 只對**存在**的維度加權，並把剩餘權重
**重新正規化**；兩個維度都缺時回傳 `None` 並標 `insufficient_evidence`。
單一維度撐起整個 composite 時，confidence 上限降為 `medium`。前端顯示 `N/A`。

### 5.6 編排

Root Agent 使用 ADK 的 `SequentialAgent`：

```
RecombineAgent → PredictAgent (Analogue Scoring) → [核准閘門] → StoryboardAgent
```

核准閘門不是 agent，是 FastAPI 層的狀態機暫停點。流程在 `awaiting_approval` 事件後掛起，收到 `POST /approve/{run_id}` 才續跑。

---

## 6. API Surface

### 6.1 Endpoints

| Method | Path | 說明 |
|---|---|---|
| `GET` | `/` | 靜態前端 |
| `POST` | `/run` | 啟動流程，回傳 `{run_id}` |
| `GET` | `/events/{run_id}` | SSE 事件流 |
| `POST` | `/approve/{run_id}` | body: `{"variant": "grounded" \| "wildcard"}` |
| `GET` | `/health` | Cloud Run 健康檢查 |

### 6.2 SSE 事件契約

```typescript
type Event =
  | { type: "agent_start";   agent: string; ts: number }
  | { type: "tool_call";     agent: string; tool: string; args: object; ts: number }
  | { type: "tool_result";   agent: string; rows: number; elapsed_ms: number;
                             preview: string[][]; ts: number }
  | { type: "tool_error";    agent: string; error: string; retry: number; ts: number }
  | { type: "agent_output";  agent: string; payload: object; ts: number }
  | { type: "awaiting_approval"; proposals: TreatmentProposal[];
                                 scores: PredictionScore[] }
  | { type: "media_ready";   scenes: SceneAsset[] }
  | { type: "done" }
  | { type: "error";         message: string }
```

**`tool_call` 事件的 `args` 必須包含 SQL 原文，`tool_result` 必須包含列數與耗時。** 這三個欄位在前端顯示，是評審驗證 runtime 呼叫最直接的證據，也是 demo 影片中最有說服力的十秒。

### 6.3 狀態管理

實作於 `app/state.py`（`RunStore` ＋ `RunState` 狀態機）與 `app/events.py`（`EventBus`）。

`running → awaiting_approval → storyboard → done`（另有 `error`）。
非法轉移會拋 `InvalidTransition`，所以亂序的 `/approve` 無法跳過閘門或復活已結束的 run。

**核准閘門是狀態轉移，不是 agent 內的 await。** agent 跑完就停，
由 FastAPI 層在收到 `POST /approve/{run_id}` 後推進狀態。
若讓 agent `await` 使用者點擊，該回合會一直開著、run 的存活綁在單一 HTTP 連線上，
而且「使用者到底核准了沒」只能從那個 stack frame 得知。

**SSE 不從 agent 內部 yield。** agent publish 到 bus，HTTP 層 subscribe。
如此 agent 可在無人觀看時執行，兩個瀏覽器可同時接同一個 run，
日後換 Redis pub/sub 只需替換 `EventBus` 實作。

> ⚠️ **部署硬性條件：單一實例。** `InProcessEventBus` 與 `RunStore` 都活在單一行程記憶體中。
> Cloud Run 若自動擴展到 2 個實例，SSE 訂閱者與 run 狀態會落在不同實例，
> **串流會靜默掛住——不報錯、不斷線、什麼都不吐**。
> 部署指令必須帶 `--min-instances=1 --max-instances=1`。

---

## 7. Media Pipeline

| 階段 | 服務 | 規格 |
|---|---|---|
| 生圖 | Imagen（`imagen-4.0-generate-001`）或 Gemini image 模型 | 16:9，3 張 |
| 風格一致性 | 固定風格前綴 prompt ＋ 相同 seed | 見下 |
| 旁白 | Cloud Text-to-Speech，Chirp 3 HD 英文聲線 | 每場景 ≤ 15 秒 |
| 儲存 | GCS bucket，公開讀取或 signed URL | `gs://{bucket}/runs/{run_id}/` |

**風格一致性作法**：所有場景 prompt 共用同一段風格前綴（如 `cinematic still, anamorphic lens, muted teal and amber palette, 35mm film grain, no text`），並固定 seed。不追求角色臉部一致——三張圖的場景不同，觀眾不會期待同一張臉。若目視結果仍不一致，降級為抽象概念構圖。

**內容安全**：prompt 中不得包含真實人名、既有影視作品名稱或角色名。母題詞彙表在設計時即應避免這類詞。

---

## 8. Configuration

### 8.1 環境變數（`.env.example`）

```bash
# ClickHouse
CLICKHOUSE_HOST=xxx.clickhouse.cloud
CLICKHOUSE_PORT=8443
CLICKHOUSE_USER=default
CLICKHOUSE_PASSWORD=
CLICKHOUSE_SECURE=true
CLICKHOUSE_DATABASE=default

# Google Cloud
GOOGLE_CLOUD_PROJECT=
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_GENAI_USE_VERTEXAI=true
GCS_BUCKET=

# Models（實作時至 Model Garden 核對）
MODEL_REASONING=
MODEL_FAST=
MODEL_IMAGE=
```

### 8.2 Secrets

Cloud Run 上一律由 Secret Manager 注入。Repo 內僅 `.env.example`，值為空。

**提交前必做**：`git log -p | grep -iE 'clickhouse.cloud|password|api[_-]key'` 掃過完整歷史。若曾誤 commit，重建 repo 而非後續刪除。

---

## 9. Repository Layout

```
.
├── LICENSE                    # Apache-2.0，About 區需可見
├── README.md
├── .env.example
├── Dockerfile
├── pyproject.toml
├── docs/
│   └── SYSTEM_SPEC.md
├── sql/
│   ├── 001_films.sql
│   ├── 002_film_attention.sql
│   └── 003_materialized_views.sql
├── etl/
│   ├── 01_wikidata_spine.py
│   ├── 02_cmu_join.py
│   ├── 03_pageviews.py
│   ├── 04_motif_enrichment.py
│   ├── 05_load_clickhouse.py
│   └── vocab.py               # 受控詞彙表
├── app/
│   ├── main.py                # FastAPI + SSE
│   ├── mcp.py                 # MCPToolset
│   ├── contracts.py           # Pydantic
│   ├── prompts.py             # system instructions + schema 說明
│   ├── media.py               # Imagen + TTS + GCS
│   └── agents/
│       ├── root.py
│       ├── recombine.py
│       ├── predict.py
│       └── storyboard.py
└── web/
    └── index.html             # 單檔，無建置流程
```

---

## 10. Observability

結構化 JSON log 至 stdout（Cloud Logging 自動採集）。每筆 tool call 記錄：`run_id`、`agent`、`tool`、`sql`、`rows`、`elapsed_ms`、`retry_count`。

**這些 log 是提交材料的一部分**——README 中附一段真實執行的 log 節錄，作為 runtime 呼叫的書面證據。

---

## 11. Non-functional Targets

| 指標 | 目標 | 備註 |
|---|---|---|
| 單次 ClickHouse 查詢 | < 500ms | MV 已預聚合 |
| Recombine + Predict 完整流程 | < 60s | demo 可接受上限 |
| Storyboard（3 張圖 ＋ 3 段音訊） | < 90s | 前端需有進度指示 |
| Cloud Run 冷啟動 | < 10s | 預裝 mcp-clickhouse 以避免下載 |
| 總 credits 消耗 | < $60 | 保留 $40 緩衝 |

---

## 12. Degradation Paths

| 失效點 | 降級方案 |
|---|---|
| MCP 於 Cloud Run 啟動失敗 | 改自架 ClickHouse（同容器 Docker） |
| LLM 產生的 SQL 反覆失敗 | 收窄工具面：system instruction 增加範例查詢；必要時只允許查 MV |
| 母題聚合結果無意義 | 縮回 genre ＋ 角色原型兩維 |
| Imagen 風格不一致 | 抽象概念構圖（色塊、剪影、構圖線） |
| 前端 SSE 不穩 | 改為輪詢 `/status/{run_id}` |
| 全面落後 | 保底線：Recombine → Predict 的 MCP 閉環 ＋ 3 張靜態圖 ＋ 旁白 |
