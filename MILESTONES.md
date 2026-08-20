# Milestone Plan

**預算**：15 個平日 × 3–5h ＋ 3 個週末 × 2h ＝ **51–58 小時**
**凍結日**：2026-09-07 晚
**截止**：2026-09-09 14:00 PDT（倫敦 22:00）

---

## 總覽

| M | 名稱 | 期間 | 時數 | 阻斷性 |
|---|---|---|---|---|
| M0 | 資格與 MCP 打通 | 8/20–8/21 | 7h | **是** — 失敗則全案停擺 |
| M1 | 資料層 | 8/22–8/27 | 16h | 是 |
| M2 | Agent 核心 | 8/28–9/2 | 16h | 是 |
| M3 | 媒體與前端 | 9/3–9/7 | 12.5h | 部分可降級 |
| M4 | 提交 | 9/8–9/9 | 7h | 是 |

**規則**：若當天只有 3 小時，砍當日任務的最後一項，**不要往後累積**。累積是這種預算下最常見的死法。

---

## M0 — 資格與 MCP 打通

> 8/20（四）+ 8/21（五），7 小時
> 這是全案唯一沒有降級方案的里程碑。

### 8/20 四（3.5h）

- [X] 送出 Google Cloud $100 credits 申請表單（**8/31 硬截止，核發需 1–5 工作天**）
- [x] 建立 public GitHub repo，首次 commit 含 `LICENSE`（Apache-2.0）→ https://github.com/erickh826/greenlight-agent
- [x] 確認 GitHub 頁面 About 區顯示授權標籤（GitHub 已自動偵測 Apache-2.0）
- [x] ClickHouse Cloud 服務開通，記下連線資訊 → https://console.clickhouse.cloud/signUp
- [x] 建立 `.env.example`，值全空
- [ ] 本機 `uv run --with mcp-clickhouse mcp-clickhouse` 能連上叢集（`uv` 已安裝；待 `.env` 填入憑證後跑 `./scripts/test_mcp_clickhouse.sh`）

**DoD**：repo 公開且授權可見；本機 MCP server 能列出資料庫。

### 8/21 五（3.5h）

- [ ] `pip install google-adk`，建立最小 agent
- [ ] MCPToolset 掛載 `mcp-clickhouse`
- [ ] Gemini 完成一次完整往返：FunctionCall → run_select_query → FunctionResponse
- [ ] **保存這段 log**（截圖 ＋ 文字），提交材料會用到

**DoD**：一段 log，內容是 Gemini 自己決定查詢並拿到 ClickHouse 回應。

### ⛔ Kill Criteria

**8/21 收工前未達成 DoD → 8/22 立即改自架 ClickHouse（Docker），不要在 Cloud 連線設定上耗第三個晚上。**

---

## M1 — 資料層

> 8/22–8/27，16 小時

### 8/22–8/23 週末（2h，低強度）

- [ ] `etl/01_wikidata_spine.py`：SPARQL 依年份分批查詢
- [ ] 下載 CMU Movie Summary Corpus
- [ ] 目視檢查兩邊的欄位與標題格式

**DoD**：`films_spine.parquet` 存在，列數 > 3000。

### 8/24 一（3.5h）

- [ ] `etl/02_cmu_join.py`：標題正規化 ＋ 年份 ±1 模糊比對
- [ ] 輸出匹配率報告

**DoD**：**有票房資料且成功匹配劇情的影片 ≥ 1500 部。**

> ⚠️ 若不足 1500：放寬上映年份至 1990 後。仍不足則放棄票房必要條件，改以 pageviews 為主要成效指標。

### 8/25 二（3.5h）

- [ ] `etl/03_pageviews.py`：REST API，每條目一次呼叫取完整區間
- [ ] Rate limit ≤ 5 req/s，含失敗重試
- [ ] 計算 `pageview_peak` 與 `pageview_decay_days`

**DoD**：`attention.parquet` 約 270 萬列；抽查 5 部片的曲線形狀合理（上映前後有明顯峰值）。

### 8/26 三（3.5h）

- [ ] `etl/vocab.py`：受控詞彙表（母題 30 個、角色原型 25 個、三幕結構 6 種）
- [ ] `etl/04_motif_enrichment.py`：Gemini Flash ＋ Pydantic response_schema
- [ ] 併發 10，指數退避重試
- [ ] 全量跑完 1500 部

**DoD**：母題欄位覆蓋率 > 95%；**隨機抽 20 部人工檢查母題是否合理**（這步不能跳過，垃圾母題會讓後面所有聚合失去意義）。

> ⚠️ 受控詞彙表是成敗關鍵。若讓 Gemini 自由生成標籤，會產出數千個近義詞，聚合完全失效。

### 8/27 四（3.5h）

- [ ] `sql/001`–`003`：建表 ＋ 三個 Materialized View
- [ ] `etl/05_load_clickhouse.py`：載入全部資料
- [ ] **手寫 5 個範例查詢驗證 MV 正確性**（特別是 `mv_motif_pair_stats` 的雙 arrayJoin 改寫）
- [ ] 記錄查詢耗時

**DoD**：能回答「2015 年後同時具備反英雄與導師原型的作品，ROI 中位數與樣本數」，且 < 500ms。

### ⛔ Kill Criteria

**8/27 晚 MV 查不出有意義結果 → 縮回 genre ＋ 角色原型兩維，放棄母題組合維度。**

---

## M2 — Agent 核心

> 8/28–9/2，16 小時

### 8/28 五（3.5h）

- [ ] `app/prompts.py`：system instruction 含完整 DDL、view 用途、3 個範例查詢
- [ ] `app/contracts.py`：Pydantic 契約
- [ ] `RecombineAgent` Phase A：MCP 自主查詢

**DoD**：agent 能自行決定查詢條件並取回聚合結果。

### 8/29–8/30 週末（2h）

- [ ] `RecombineAgent` Phase B：關閉 tools，開 `response_schema` 收斂
- [ ] 彩蛋分支：一次 `temperature=1.5` 額外呼叫

**DoD**：輸出兩個 `TreatmentProposal`（grounded ＋ wildcard）。

### 8/31 一（3.5h）

- [ ] `PredictAgent`：自主組裝查詢條件（年份、預算區間、母題交集）
- [ ] SQL 錯誤重試迴圈（最多 2 次）

**DoD**：能取回同類作品的 ROI 分位數與注意力特徵。

### 9/1 二（3.5h）

- [ ] 可解釋評分邏輯：`commercial_score` ＋ `attention_score` → `composite`
- [ ] `EvidenceItem` 填充：每個數字附帶產生它的 SQL
- [ ] `insufficient_evidence` 分支（樣本數 < 8）

**DoD**：評分附帶證據列表，不是黑箱數字。

### 9/2 三（3.5h）

- [ ] Root Agent 編排（`SequentialAgent`）
- [ ] 端到端 CLI 跑通
- [ ] 結構化 log 輸出

**DoD**：一次執行輸出雙方案 JSON ＋ 完整 tool call trace。

### ⛔ Kill Criteria

**9/2 晚端到端未通 → 砍彩蛋方案，只保留單一方案路徑。**

---

## M3 — 媒體與前端

> 9/3–9/7，12.5 小時

### 9/3 四（3.5h）

- [ ] `app/media.py`：Imagen 生圖，固定風格前綴 ＋ seed
- [ ] GCS 上傳與 URL 產生
- [ ] `StoryboardAgent`：大綱 → 3 場景描述 → 生圖

**DoD**：3 張風格一致的 16:9 圖片，可由 URL 存取。

### 9/4 五（3.5h）

- [ ] Cloud TTS 旁白（Chirp 3 HD）
- [ ] 音訊上傳 GCS，取得 duration
- [ ] `SceneAsset` 契約填充完成

**DoD**：圖 ＋ 音訊可播放。

### 9/5–9/6 週末（2h）

- [ ] `web/index.html` 骨架
- [ ] SSE 接收與事件渲染
- [ ] **證據流區塊：顯示 SQL 原文、列數、耗時**

**DoD**：瀏覽器能即時看到 agent 的查詢過程。

### 9/7 一（3.5h）

- [ ] 雙方案對比區塊 ＋ 核准按鈕
- [ ] CSS Ken Burns 播放器（`transform: scale` ＋ `translate`，`audio.timeupdate` 切換場景）
- [ ] Dockerfile ＋ Cloud Run 部署
- [ ] Secret Manager 設定
- [ ] **無痕視窗完整測試一次**

**DoD**：公開 URL 可用，陌生人能自行跑完一次。

### 🔒 當晚凍結。9/8 之後只修會導致 demo 崩潰的 bug。

### ⛔ Kill Criteria

- 9/4 晚生圖未完成 → 改抽象概念構圖
- 9/7 晚前端未完成 → hosted URL 降為靜態結果頁 ＋ 一個 demo 觸發按鈕，放棄 SSE

---

## M4 — 提交

> 9/8–9/9，7 小時

### 9/8 二（3.5h）

- [ ] 錄製 demo（腳本應在 M3 期間的等待空檔先寫好）
- [ ] 剪輯：0:00–0:15 自產分鏡開場 → 0:15–2:30 功能演示 → 2:30–2:50 定位陳述
- [ ] **演示段必須包含：Gemini 產生的 SQL 畫面、ClickHouse 回應、雙方案對比、分鏡播放**
- [ ] 英文字幕
- [ ] 上傳 YouTube 並設為**公開**（非「不公開」）
- [ ] README：架構圖、資料源授權、分析邊界聲明、執行步驟、log 節錄

**DoD**：影片公開可見；README 完整。

### 9/9 三（3.5h，緩衝）

- [ ] `git log -p | grep -iE 'clickhouse.cloud|password|api[_-]key'` 掃描歷史
- [ ] 依提交檢查清單逐項確認
- [ ] Devpost 表單填寫，選定 ClickHouse 賽道
- [ ] **提交後再次以無痕視窗驗證三個 URL**（repo、hosted、影片）

> 目標是 9/8 晚就完成提交，9/9 只處理意外。

---

## 提交檢查清單

- [ ] Repo 公開，OSI 授權在 **About 區**可見
- [ ] 首次 commit 時間在 2026-07-27 之後
- [ ] 依賴清單 / Dockerfile / `.env.example` / **commit 歷史**均無非 Google 的 AI 服務
- [ ] 憑證未進入 repo（已掃描歷史）
- [ ] 程式碼中可見 `google-adk` ＋ `mcp-clickhouse` 的實際 runtime 呼叫
- [ ] Hosted URL 可從無痕視窗存取並完整跑完一次
- [ ] 影片 ≤3 分鐘、YouTube 公開、英文字幕、畫面含 SQL 與 ClickHouse 回應
- [ ] README 含架構圖、三個資料源授權聲明、抽象分析邊界聲明
- [ ] 平台名稱使用 **Gemini Enterprise Agent Platform**（非 Vertex AI）
- [ ] Devpost 表單已選定 ClickHouse 賽道

---

## 不做清單

以下項目在此預算下無法容納。若在 9/2 前意外提早完成，唯一值得加回的是**擴大資料集至 5000 部**——更多資料讓現有展示更有說服力，新功能只會增加崩潰機率。

- ClickHouse 向量索引與混合檢索
- Veo 影片片段
- Context Caching
- 第四個 runtime sub-agent
- 後端影片合成（moviepy / FFmpeg）
- 超過 3 個場景
- 使用者帳號、持久化 session
- 共用工程 foundation、第二場賽事
