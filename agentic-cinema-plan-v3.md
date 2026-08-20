# Agentic Cinema：ClickHouse 賽道執行計畫 v3

**基準日**：2026-08-20
**截止**：2026-09-09 14:00 PDT（倫敦時間 22:00）
**路線**：B — 保留原創作概念，資料源全面換為 CC0 / 維基來源
**預算**：15 個平日 × 3–5h ＋ 3 個週末 × 2h ＝ **51–58 小時**（以 51 小時規劃）

---

## 0. 這份計畫與 v2 的差異

v2 是在假設有充裕時間下寫的。v3 以 51 小時的實際預算重新分配，做了三個結構性削減與一個架構改動。**被刪除的項目列在 §8，請不要在執行期間把它們加回來。**

---

## 1. 架構改動：拆解 Agent 下放到 ETL

### 原設計的問題

四個 runtime sub-agent 中，「拆解」在每次執行時即時分析劇情文本。這造成三個成本：多一個 agent 要開發與測試、demo 延遲增加十幾秒、而 ClickHouse 裡沒有可聚合的結構欄位。

### 新設計

拆解改為**離線批次 enrichment**：ETL 階段對 1500 部片各跑一次 Gemini Flash，輸出結構化母題，直接寫成 ClickHouse 欄位。

```sql
CREATE TABLE films
(
    film_id             String,
    title               String,
    release_year        UInt16,
    genres              Array(String),
    budget_usd          Nullable(UInt64),
    revenue_usd         Nullable(UInt64),
    roi                 Nullable(Float32),      -- 物化欄位，revenue/budget
    -- 以下四欄由 Gemini 於 ETL 階段產生
    motif_tags          Array(String),          -- 抽象結構母題
    act_structure       LowCardinality(String), -- 三幕結構型態分類
    character_archetypes Array(String),         -- 角色原型
    tone_axis           Float32,                -- 冷峻 ↔ 溫暖，-1 到 1
    -- 觀眾注意力
    pageview_peak       Nullable(UInt32),
    pageview_decay_days Nullable(UInt16)        -- 峰值衰減至 50% 所需天數
)
ENGINE = MergeTree
ORDER BY (release_year, film_id);
```

### 為什麼這是本專案最關鍵的一步

因為它讓 agent 問得出這種問題：

> 「2015 年後，同時具備『反英雄主角』與『導師原型』的作品，其 ROI 中位數與注意力衰減速度的關係為何？預算區間 $30–60M 內樣本數是否足夠？」

對 genre 做 GROUP BY 是任何資料庫都做得到的展示。對**陣列欄位交集 × 多維度 × 大量事件表 JOIN** 做即時聚合，才是 ClickHouse 的主場。這是 Technological Implementation 與 Quality of the Idea 兩項的共同著力點。

**成本**：1500 次 Gemini Flash 呼叫，約 30 分鐘、數美元。用 `google-genai` 的 batch 或簡單併發即可。

### Runtime 剩下三個 Agent

| Agent | 職責 | 工具 | 終止條件 |
|---|---|---|---|
| **重組 Agent** | 自主查 ClickHouse 判斷當前潛力母題組合，產出新故事大綱；額外一次高 temperature 呼叫產出彩蛋方案 | `mcp-clickhouse` | 輸出兩個大綱物件 |
| **預測 Agent** | 自主組裝查詢條件，取回同類作品的 ROI 分布與注意力曲線，計算可解釋評分 | `mcp-clickhouse` | 樣本數 < 8 時回傳 `insufficient_evidence`，不硬猜 |
| **分鏡 Agent** | 選定方案 → 3 個關鍵場景描述 → Imagen 生圖 → Cloud TTS 旁白 | Imagen、Cloud TTS、GCS | 3 組圖＋音訊 URL |

Root Agent 用 ADK 編排。三個 agent 各有不可混淆的職責與最小工具權限，這比四個職責重疊的 agent 更容易在影片裡講清楚。

---

## 2. 資料層（全部 CC0 / 維基來源）

| 來源 | 內容 | 授權 | 取得成本 |
|---|---|---|---|
| **CMU Movie Summary Corpus** | 42,306 部維基百科劇情摘要 ＋ metadata | 維基百科衍生（CC BY-SA） | 單一 tar 下載，~45MB |
| **Wikidata SPARQL** | 票房、預算、類型、上映日 | **CC0** | 依年份分批查詢，避免 timeout |
| **Wikimedia Pageviews API** | 每部片維基條目的每日瀏覽量 | **CC0** | 每條目一次呼叫即回傳完整區間 |

### 篩選與規模

從 CMU corpus 中篩出 **1500 部**（有票房資料、2000 年後、英語為主）作為主資料集。Pageviews 抓 2015-07 起（API 起始點）至今的日粒度資料 → 約 **270 萬列**事件表。

```sql
CREATE TABLE film_attention
(
    film_id  String,
    date     Date,
    views    UInt32
)
ENGINE = MergeTree
ORDER BY (film_id, date);
```

### Materialized View（給 LLM 查的聚合層）

建三個具名 view，讓 agent 查現成聚合而非自行拼裝複雜 SQL：

- `mv_archetype_performance` — 角色原型組合 × 年份區間 → ROI 分位數、樣本數
- `mv_motif_pair_stats` — 母題兩兩組合 → 平均 ROI、注意力峰值中位數、樣本數
- `mv_attention_curve` — 每片上映後 90 天的注意力衰減特徵

### 版權處理

**不要把 CMU corpus 的劇情原文存進公開 repo 或 ClickHouse。** 只存 Gemini 產出的抽象母題欄位。ETL 腳本在執行時才下載原始語料。README 中列明三個資料源與授權，並聲明「本專案僅儲存抽象結構特徵，不儲存或輸出任何劇情原文」。

這樣做同時解決授權與版權兩件事，而且比 v1 的 TMDB 方案更容易在影片裡用一句話講完。

---

## 3. 逐日排程

> 平日格以 3.5 小時計。若當天只有 3 小時，砍當日任務的最後一項，不要往後累積。

| 日期 | 時數 | 任務 | 當日完成判準 |
|---|---|---|---|
| **8/20 四** | 3.5 | 送出 GCP credits 表單；建 public repo ＋ LICENSE（確認 About 區顯示）；ClickHouse Cloud 開通 | repo 公開、License 標籤可見 |
| **8/21 五** | 3.5 | `mcp-clickhouse` 本地跑通；ADK `McpToolset` 掛載；Gemini 完成一次 FunctionCall 往返 | **保留一段 tool call log。這是全案唯一無降級方案的關卡** |
| 8/22–23 六日 | 2 | 下載 CMU corpus；寫 Wikidata SPARQL 分年查詢腳本並跑 | 本地有 films.csv |
| **8/24 一** | 3.5 | Pageviews API 抓取腳本；1500 條目跑完 | 本地有 270 萬列 parquet |
| **8/25 二** | 3.5 | ClickHouse 建表；載入 films ＋ film_attention | Cloud 上可查詢 |
| **8/26 三** | 3.5 | 拆解 ETL：1500 次 Gemini Flash，Pydantic schema 綁定，寫回母題欄位 | 母題欄位覆蓋率 > 95% |
| **8/27 四** | 3.5 | 三個 Materialized View；手寫 5 個範例查詢驗證 | MV 回答得出兩個母題組合問題 |
| **8/28 五** | 3.5 | 重組 Agent：MCP 自主查詢 ＋ 兩段式呼叫（ReAct 段開 tools，收斂段開 response_schema） | 能產出一個結構化大綱 |
| 8/29–30 六日 | 2 | 彩蛋分支（高 temperature 額外呼叫）；SQL 錯誤重試迴圈 | 兩個方案並存 |
| **8/31 一** | 3.5 | 預測 Agent：自主組裝查詢條件、取回分布 | 拿得到同類作品 ROI 分位數 |
| **9/1 二** | 3.5 | 可解釋評分邏輯；`insufficient_evidence` 分支 | 分數附帶證據來源清單 |
| **9/2 三** | 3.5 | Root Agent 編排；端到端跑通 CLI | 一次執行輸出雙方案 JSON |
| **9/3 四** | 3.5 | 分鏡 Agent：3 場景 → Imagen 生圖 → GCS | 有 3 張風格一致的圖 |
| **9/4 五** | 3.5 | Cloud TTS 旁白；資產 URL 串接 | 圖＋音訊可播 |
| 9/5–6 六日 | 2 | 前端骨架：單頁 HTML ＋ SSE 接收 agent 事件流 | 能即時顯示 SQL 查詢過程 |
| **9/7 一** | 3.5 | 前端完成：雙方案並列 ＋ CSS Ken Burns 播放器；Cloud Run 部署 | **公開 URL 可用；當晚凍結** |
| **9/8 二** | 3.5 | 錄影、剪輯、英文字幕、上傳 YouTube；README | 影片公開可見 |
| **9/9 三** | 3.5 | Devpost 表單、最終檢查、緩衝 | 提交完成 |

**凍結日 9/7 晚。** 9/8 之後只修會導致 demo 崩潰的 bug，不加功能。

---

## 4. 每階段的 Kill Criteria

| 檢查點 | 若未達成 | 應對 |
|---|---|---|
| 8/21 晚 MCP 未跑通 | — | 8/22 立即改自架 ClickHouse（Docker），放棄 Cloud |
| 8/27 晚 MV 查不出有意義結果 | 母題品質不足 | 縮回 genre ＋ 角色原型兩維，放棄母題組合 |
| 9/2 晚端到端未通 | 落後 | 砍彩蛋方案，只保留單一方案路徑 |
| 9/4 晚生圖未完成 | 落後 | 改用統一風格的抽象概念圖（色塊構圖），不追求角色寫實 |
| 9/7 晚前端未完成 | 嚴重落後 | **保底線**：hosted URL 改為靜態結果頁 ＋ 一個可觸發的 demo 按鈕，放棄 SSE 串流 |

**絕對保底線**：「重組 → 預測」的 MCP 資料閉環 ＋ 三張靜態圖 ＋ 旁白。這條線是賽道契合度的全部，其餘皆可捨。

---

## 5. 前端規格（你自己寫，Design 佔 25%）

不要引入建置流程。單一 `index.html`，原生 JS，由 FastAPI 以 static 掛載。

**三個畫面區塊，一頁到底：**

1. **證據流（上）** — SSE 即時串流 agent 事件。**必須把 Gemini 產生的 SQL 原文與 ClickHouse 回傳的列數／耗時顯示出來。** 這是評審驗證「runtime 呼叫」最直接的證據，也是影片最有說服力的十秒。
2. **雙方案對比（中）** — 穩健方案 vs 彩蛋方案並列，各自附評分與**支撐該分數的證據列表**（哪幾個母題、多少樣本、ROI 分位數）。
3. **動態分鏡播放器（下）** — CSS `transform: scale()` ＋ `translate()` 做 Ken Burns，配合 `<audio>` 的 `timeupdate` 事件切換場景。

第 3 塊約 2 小時可完成，不要在這裡追求精緻。第 1 塊才是分數所在。

---

## 6. 產品敘事（寫進 Devpost description 與影片開場）

> 獨立製片與開發主管在 greenlight 前，靠直覺與零散報導判斷「這個題材組合現在還有市場嗎」。本專案把四萬部電影拆解為抽象結構母題，存入 ClickHouse，讓 agent 對「角色原型組合 × 觀眾注意力曲線 × 投資回報」做即時多維聚合，提出**可辯護**的題材提案——每個分數都附帶它所依據的歷史證據。同時輸出一個刻意高風險的對照方案，用來檢驗評分模型是否只會獎勵安全選項。

彩蛋方案必須用「對照組」框架呈現。沒有這層框架，它在 Potential Impact 一項會被判為 gimmick。

---

## 7. 提交檢查清單（9/9 逐項確認）

- [ ] Repo 公開，OSI 授權在 **About 區**可見
- [ ] 首次 commit 時間在 2026-07-27 之後
- [ ] `requirements.txt` / Dockerfile / `.env.example` / **commit 歷史**均無 fal.ai、ElevenLabs 或任何非 Google AI 依賴
- [ ] ClickHouse 憑證走 Secret Manager，repo 內僅 `.env.example`；用 `git log -p` 掃過一次
- [ ] 程式碼中可見 `google-adk` ＋ `mcp-clickhouse` 的實際 runtime 呼叫
- [ ] Hosted URL 可從無痕視窗存取並完整跑完一次
- [ ] 影片 ≤3 分鐘、YouTube 公開、英文字幕、**畫面包含 SQL 與 ClickHouse 回應**
- [ ] README 含架構圖、三個資料源授權聲明、抽象分析邊界聲明、執行步驟
- [ ] Devpost 表單選定 ClickHouse 賽道
- [ ] 平台名稱使用 **Gemini Enterprise Agent Platform**（非 Vertex AI）

---

## 8. 已刪除項目（不要加回來）

以下項目在 51 小時預算下無法容納，且刪除它們不影響任何一項評分的及格線：

- ClickHouse 向量索引與混合檢索
- Veo 3.1 影片片段
- Vertex AI Context Caching
- 第四個 runtime sub-agent
- moviepy / FFmpeg 後端影片合成
- 5 個場景（降為 3 個）
- 共用工程 foundation / 第二場賽事

若在 9/2 前意外提早完成所有進度，唯一值得加回的是**擴大資料集至 5000 部**，而不是新增功能。更多資料讓現有的聚合展示更有說服力；新功能只會增加 demo 崩潰的機率。

---

## 附註：需在實作時確認的模型 ID

Model Garden 內的名稱在 2026 年變動頻繁，寫進 README 前請核對：推理（Gemini 3 Pro / 2.5 Pro 系）、批次拆解（Gemini Flash 系）、生圖（`imagen-4.0-generate-001` 或 `gemini-3-pro-image`）、TTS（Cloud Text-to-Speech，Chirp 3 HD 聲線）。
