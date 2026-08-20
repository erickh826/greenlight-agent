# System Specification

**Project**: Agentic Cinema — ClickHouse Track
**Version**: 1.0（基於執行計畫 v3）
**Date**: 2026-08-20

---

## 1. Scope

### 1.1 目標

一個三層 agent 系統：對四萬部歷史電影的抽象結構母題做即時多維聚合，產出有歷史證據支撐的新題材提案，並生成可用於 pitch 的動態分鏡。

### 1.2 Non-goals（明確不做）

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
│  │  ├─ RecombineAgent  ─┐                     │  │
│  │  ├─ PredictAgent    ─┼─ MCPToolset         │  │
│  │  └─ StoryboardAgent  │  （核准後才執行）    │  │
│  ├──────────────────────┼─────────────────────┤  │
│  │ mcp-clickhouse（stdio 子行程，同容器）      │  │
│  └──────────────────────┼─────────────────────┘  │
└─────────────────────────┼────────────────────────┘
          ┌───────────────┴────────┬──────────────┐
          ▼                        ▼              ▼
   ClickHouse Cloud        Gemini / Imagen /    GCS
   （films + attention）   Cloud TTS            （媒體資產）
```

**離線 ETL** 不在此容器內，由本機執行後寫入 ClickHouse（見 §4）。

---

## 3. Data Model

### 3.1 `films`

```sql
CREATE TABLE films
(
    film_id              String,              -- Wikidata QID，如 'Q25188'
    enwiki_title         String,              -- 英文維基條目標題，用於 pageviews
    title                String,
    release_year         UInt16,
    genres               Array(LowCardinality(String)),
    budget_usd           Nullable(UInt64),
    revenue_usd          Nullable(UInt64),
    roi                  Nullable(Float32) MATERIALIZED
                            if(budget_usd > 0, revenue_usd / budget_usd, NULL),

    -- ETL 階段由 Gemini 產生（見 §4.4）
    motif_tags           Array(LowCardinality(String)),
    act_structure        LowCardinality(String),
    character_archetypes Array(LowCardinality(String)),
    tone_axis            Float32,             -- -1 冷峻 ↔ +1 溫暖
    conflict_scale       LowCardinality(String), -- personal / communal / existential

    -- 注意力特徵（由 film_attention 派生）
    pageview_peak        Nullable(UInt32),
    pageview_decay_days  Nullable(UInt16),

    ingested_at          DateTime DEFAULT now()
)
ENGINE = MergeTree
ORDER BY (release_year, film_id);
```

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

規模約 270 萬列（1500 部 × 約 1800 天）。

### 3.3 Materialized Views

三個具名 view。**agent 只查這三個 view 與 `films`，不查 `film_attention` 原始表**——把 SQL 難度前移到 schema 設計，是 LLM 產出可執行查詢的關鍵。

```sql
-- 角色原型組合表現
CREATE MATERIALIZED VIEW mv_archetype_performance
ENGINE = AggregatingMergeTree
ORDER BY (archetype, release_year)
AS SELECT
    arrayJoin(character_archetypes) AS archetype,
    release_year,
    countState()                    AS sample_count,
    quantileState(0.5)(roi)         AS roi_median,
    quantileState(0.75)(roi)        AS roi_p75,
    avgState(tone_axis)             AS avg_tone
FROM films
WHERE roi IS NOT NULL
GROUP BY archetype, release_year;

-- 母題兩兩組合
CREATE MATERIALIZED VIEW mv_motif_pair_stats
ENGINE = AggregatingMergeTree
ORDER BY (motif_a, motif_b)
AS SELECT
    motif_a, motif_b,
    countState()                       AS sample_count,
    quantileState(0.5)(roi)            AS roi_median,
    quantileState(0.5)(pageview_peak)  AS attention_peak_median
FROM (
    SELECT
        arrayJoin(motif_tags) AS motif_a,
        arrayJoin(motif_tags) AS motif_b,
        roi, pageview_peak
    FROM films
    WHERE roi IS NOT NULL
)
WHERE motif_a < motif_b
GROUP BY motif_a, motif_b;

-- 上映後 90 天注意力曲線
CREATE MATERIALIZED VIEW mv_attention_curve
ENGINE = AggregatingMergeTree
ORDER BY (film_id, days_since_peak)
AS SELECT
    film_id,
    dateDiff('day', peak_date, date) AS days_since_peak,
    sumState(views)                  AS views
FROM film_attention
-- peak_date 於 ETL 階段預先計算並寫入輔助表
GROUP BY film_id, days_since_peak;
```

> 註：`mv_motif_pair_stats` 的雙重 `arrayJoin` 在 ClickHouse 中會產生笛卡兒積，實作時需以 `arrayJoin(arrayMap(...))` 或先物化 pair 陣列的方式改寫。8/27 的驗證步驟必須實測此 view 的正確性。

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

**輸出**：`films_spine.parquet`，預期 5000–15000 列

### 4.2 `02_cmu_join.py`

CMU Movie Summary Corpus 提供劇情文本。

- 下載 `MovieSummaries.tar.gz`，使用 `plot_summaries.txt` 與 `movie.metadata.tsv`
- **Join 風險**：CMU 用 Wikipedia page ID 與 Freebase ID，與 Wikidata QID 無直接對應。實務作法為正規化標題（小寫、去標點、去 "The"）＋ 上映年份 ±1 的模糊比對
- 預期匹配率 70–85%
- **Gate**：成功匹配且有票房資料者 ≥ 1500 部才可進入下一步。若不足，放寬年份至 1990 後

**輸出**：`films_with_plots.parquet`

### 4.3 `03_pageviews.py`

- Endpoint: `https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/en.wikipedia/all-access/user/{title}/daily/{start}/{end}`
- **每個條目一次呼叫即回傳完整日期區間**，故 1500 部片僅需約 1500 次呼叫
- 起始日期 20150701（API 資料起點），終止為今日
- 加入 rate limit（每秒 ≤ 5 次）與失敗重試
- 同時計算 `pageview_peak` 與 `pageview_decay_days` 寫回 films
- 授權：CC0

**輸出**：`attention.parquet`（約 270 萬列）

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
- 成本估算：1500 次 Flash 呼叫 ≈ $1–3

**輸出**：`motifs.parquet`

### 4.5 `05_load_clickhouse.py`

- 使用 `clickhouse-connect`（原生 driver，僅用於寫入，不涉及 runtime）
- 建表 → 載入 → 建 MV → 驗證
- 冪等：以 `TRUNCATE` 後重載，而非 upsert

### 4.6 資料授權處理

**劇情原文不進入 repo 也不進入 ClickHouse。** ETL 執行時才下載 CMU corpus，僅將衍生的母題欄位寫入資料庫。README 列明三個來源與授權，並聲明分析邊界。

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
| **PredictAgent** | 兩個 proposal 的母題與原型 | clickhouse_tools | `PredictionScore × 2` | 樣本數 < 8 → `insufficient_evidence` |
| **StoryboardAgent** | 經核准的單一 proposal | Imagen、TTS、GCS | `SceneAsset × 3` | 3 組圖＋音訊 URL |

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
    proposal_title: str
    commercial_score: float           # 0–100
    attention_score: float            # 0–100
    composite: float
    confidence: Literal["high", "medium", "low", "insufficient_evidence"]
    evidence: list[EvidenceItem]
    caveats: list[str]

class SceneAsset(BaseModel):
    scene_index: int
    description: str
    image_url: str                    # GCS
    audio_url: str                    # GCS
    duration_sec: float
```

**評分必須可解釋**：`composite` 不是黑箱數字，而是由 `evidence` 列表中的具體查詢結果組成。前端要把兩者並列顯示。

### 5.6 編排

Root Agent 使用 ADK 的 `SequentialAgent`：

```
RecombineAgent → PredictAgent → [核准閘門] → StoryboardAgent
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

單一 Cloud Run 執行個體，記憶體內 dict 保存 `run_id → state`。不使用 Firestore、不做持久化。實例重啟即失效，這對 demo 是可接受的。

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
