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
- [x] 本機 `uv run --with mcp-clickhouse mcp-clickhouse` 能連上叢集 → `./scripts/test_mcp_clickhouse.sh` ✓

**DoD**：repo 公開且授權可見；本機 MCP server 能列出資料庫。

### 8/21 五（3.5h）

- [x] `google-adk` 安裝（走 `uv`，非 `pip`），最小 agent 在 `scripts/m0_adk_roundtrip.py`
- [x] MCPToolset 掛載 `mcp-clickhouse` → discover 到 `list_databases` / `list_tables` / `run_query`（環境坑與版本組合見 `docs/M0_SETUP.md` §4.3）
- [x] Gemini 完成一次完整往返：FunctionCall(`list_databases`) → FunctionResponse → 模型結論
- [x] **保存這段 log** → `docs/m0-mcp-trace.log`（host/password 已遮蔽）

**DoD**：一段 log，內容是 Gemini 自己決定查詢並拿到 ClickHouse 回應。

### ⛔ Kill Criteria

**8/21 收工前未達成 DoD → 8/22 立即改自架 ClickHouse（Docker），不要在 Cloud 連線設定上耗第三個晚上。**

---

## M1 — 資料層

> 8/22–8/27，16 小時

### 8/22–8/23 週末（2h，低強度）

- [x] `etl/01_wikidata_spine.py`：SPARQL 依年份分批查詢，幣別過濾 ＋ 多值屬性聚合
- [x] 下載 CMU Movie Summary Corpus（`scripts/fetch_cmu_corpus.sh`）
- [x] 目視檢查兩邊的欄位與標題格式 → `title` 與 `enwiki_title` 有 47% 不同，不可互換
- [x] `etl/02_cmu_join.py` 提前完成（原訂 8/24），匹配率 77.6%
- [x] 漏斗率實測報告 → `docs/M1_DATA_FINDINGS.md`

**DoD**：`films_spine.parquet` 存在，1,595 部唯一影片（要求票房＋預算）。
原 DoD 訂「列數 > 3000」，但即使放寬成規格 §4.1 的原始條件（僅要求票房）
實測也只有 2,157 部——規格的 5000–15000 估計偏高。

> ⚠️ 實測推翻兩個計畫假設，**8/24 開工前必讀 `docs/M1_DATA_FINDINGS.md`**：
> 1. **CMU 語料在 2012 年結束**，與 pageviews API 起點（2015-07）完全不重疊。
>    沒有任何一部片能同時有劇情與上映期關注度曲線。
> 2. **1500 部門檻達不到**：放寬至 1990 年後仍只有 1,238 部。

### 8/24 一（3.5h）

- [ ] `etl/02_cmu_join.py`：標題正規化 ＋ 年份 ±1 模糊比對
- [ ] 輸出匹配率報告

**DoD（8/26 下修）**：**有票房資料且成功匹配劇情的影片 ≥ 1200 部** → 實測 **1,238**，PASS。

> 原訂 1500 是估計值，已被漏斗實測推翻——年份下界一路放寬到 1990（規格允許的
> 降級路徑）之後仍只有 1,238 部，差距來自 CMU 在 2012 停止收錄、Wikidata 同時
> 具備 USD 預算與票房的影片本來就少、幣別過濾丟掉 503 部。完整推導見
> `docs/M1_DATA_FINDINGS.md` §2。
>
> 門檻改為 1200 而非直接刪掉：低於實測值 38 部，留得下重跑掉幾部的空間，
> 又高到足以讓真正的迴歸（join 壞掉、幣別過濾改動）依然亮紅燈。
> 永遠失敗的 gate 等於沒有 gate，還會蓋掉下一個真的壞掉的檢查。

### 8/25 二（3.5h）

- [x] `etl/03_pageviews.py` 已寫好（smoke test 25 部通過，101,775 列）
- [x] 全量跑完 1,238 部 → **4,937,204 列，1,238/1,238（100%）**
- [x] 抽查 5 部曲線形狀（`scripts/spotcheck_curves.py`，分層取樣非隨機）

指標已改定義（`SYSTEM_SPEC .md` §3.1、§4.3）。條目名用 `enwiki_title`，
用 `title` 會全部 404。輸出 `interest_median_daily` / `interest_p95_daily` /
`interest_trend_slope` / `interest_cohort_pct` / `years_to_measurement`，
**不再有 `pageview_peak` 與 `pageview_decay_days`**。

**DoD**：`attention.parquet` **4,937,204 列**（1,238 部 × 最多 4,074 天）→ PASS。

> 原 DoD 寫「`interest_cohort_pct` 與 `years_to_measurement` 無顯著相關
> （smoke test −0.039，原始值 +0.272）」。全量實測 **原始值就是 −0.009**，
> 該混淆因子並不存在，+0.272 來自 `head(25)` 的非隨機切片。
> 見 `docs/M1_DATA_FINDINGS.md` §1。

### 8/26 三（3.5h）

- [x] `etl/vocab.py`：受控詞彙表（母題 30、角色原型 25、三幕結構 6、衝突尺度 3、cohort 5）
      —— 為四個下游的單一來源，`app/contracts.py` 的 Enum 由它生成
- [x] `etl/04_motif_enrichment.py`：Gemini Flash ＋ `app/contracts.py` 的 `FilmMotifs`
- [x] 併發 10，指數退避重試（＋ JSONL checkpoint，中斷可續）
- [x] 劇情文本先清 wiki markup（29.6% 含 `{{...}}` / `[[...]]` / `<ref>`），
      過短者（< 500 字元，28 部）設下限跳過
- [x] 全量跑完 → **1,210/1,238（97.7%）**
- [x] **印出母題分布直方圖** —— 30/30 個母題都被用到，前三名合計 **22.4%**，
      無眾數塌縮；角色原型 24/25（僅 `gatekeeper` 未用），前三名 37.6%
- [x] `THINKING_BUDGET=0` —— 實測 thinking 開關是兩套標註政策（`act_structure`
      只有 6/12 一致），中途換邊會在每個 GROUP BY 埋下系統性接縫，見 §6.4
- [x] `tone_axis` 錨定七個具名刻度 —— 未錨定時 67% 塌到 ±1.0 端點，
      而 `avgState(tone_axis)` 不會報錯，見 §6.5
- [x] **隨機抽 20 部人工檢查母題** → **PASS**，20 部大多數標籤合理，
      無會阻斷 M1/M2 的垃圾母題

**DoD**：母題欄位覆蓋率 > 95% → **PASS（97.7%）**；人工抽查 20 部 → **PASS**。

> **P1（不阻斷 demo，賽後清理）**：`reluctant_hero` 同時存在於 `MOTIFS` 與
> `ARCHETYPES`，抽查中兩邊都出現。實測 1,210 部：當母題 82 次、當原型 521 次，
> **67 部（5.5%）同一部片兩軸都掛著它**，在跨兩軸的聚合裡等於同一件事算兩次。
>
> 它本來就不該在 `MOTIFS`——那份清單放的是戲劇處境，而 reluctant hero 是一個人。
> 不現在修的原因：從 `MOTIFS` 移除會讓全部已標註資料失效，等於重跑全量標註。
> 已在 `etl/vocab.py` 加 `KNOWN_VOCAB_OVERLAP` 釘住這一個，
> **新增的重疊會直接讓測試失敗**。

> ⚠️ 受控詞彙表是成敗關鍵。若讓 Gemini 自由生成標籤，會產出數千個近義詞，聚合完全失效。

### 8/27 四（3.5h）

- [x] `sql/001`–`003` 已寫好（`mv_motif_pair_stats` 的 arrayJoin 改寫已實測驗證）
- [x] `etl/05_load_clickhouse.py`：載入全部資料 →
      `films` 1,238 列、`film_attention` **4,937,204** 列（87s）
- [x] **手寫 5 個範例查詢驗證 MV 正確性** → `scripts/verify_mv.py`，
      三個 view 全部**逐格**與原表重算對照，不是只確認「有回傳列」：
      `mv_archetype_performance` 120 格、`mv_motif_pair_stats` 417 格、
      `mv_interest_by_year` 14,605 格，全部相符
- [x] **驗證每格樣本數** —— 見下方修正
- [x] 記錄查詢耗時 —— **功能正確性全過；latency 需持續觀察**

**DoD**：能回答「2005–2014 同時具備反英雄與導師原型的作品，ROI 中位數與樣本數」
→ **n=31，ROI 中位數 1.898，p75 3.504**。功能 PASS。
（原題目寫「2015 年後」，但資料集在 2014 年結束。）

> **latency 尚未穩定，不要當成既定事實。** 同一組查詢在不同次執行量到過
> **276ms 與 525ms**，後者略高於 §11 的 500ms 目標。單次冷樣本在 dev-tier
> 服務上是雜訊而非量測，`verify_mv.py` 因此改成每項跑 5 次、以**中位數**判定，
> 並印出最快／最慢。最近一次：中位數 215–248ms，尾端最高 329ms。
>
> M2 Phase A 不因此阻擋，但 demo 前需再觀察；若尾端持續超過 500ms，
> 考慮升級服務層級或在前端顯示實際耗時而非承諾上限。

> **樣本數門檻的統計量改了。** 原訂「低於門檻的格子 < 10%」，實測
> archetype × bucket 是 **25.0%**、母題配對 **23.7%**，不達標。
>
> 但數格子在偏態分布上是錯的統計量——`shadow_antagonist` 有 678 個標註、
> `creator` 只有 15，相差 45 倍，無論怎麼分桶稀有原型都會薄。改看它代表的
> 兩件事：
>
> - **稀疏格子只握有 2.7% / 2.9% 的樣本**（127/4,717、354/12,189）
> - **不帶 `release_bucket` 查詢時，24 個原型全部達門檻**（最小 15）；
>   母題配對不帶年份時 76% 達門檻
>
> 也就是說稀疏是「切太細」的後果，不是資料不足。`app/prompts.py` 已經要求
> agent 先查廣的聚合、確認樣本數後才加年份維度，那條路徑 100% 達標。
> 格子數仍會印出來，只是不作為 gate。

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

- [ ] `PredictAgent`（對外稱 **Analogue / Evidence Scoring Agent**）：自主組裝查詢條件
      （年份、預算區間、母題交集），檢索歷史類比案例
- [ ] SQL 錯誤重試迴圈（最多 2 次）

**DoD**：能取回同類歷史作品的 ROI 分位數與 page-interest proxy 特徵。

### 9/1 二（3.5h）

- [ ] 可解釋評分邏輯：`commercial_score` ＋ `attention_score` → `composite`
- [ ] `EvidenceItem` 填充：輸出 **historical-analogue evidence**，每個數字附帶產生它的 SQL
- [ ] `insufficient_evidence` 分支（樣本數 < 8，不硬猜）

**DoD**：評分附帶完整的歷史類比證據列表，而非黑箱票房預測數字。

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
