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

> **latency 是雙峰，不是單一數值上下抖動。** 之前寫「單次 525ms 是雜訊」
> 講得太寬鬆——實測**閒置服務**上跑 5 次的中位數是 **522ms 與 508ms、尾端
> 1047ms**，那不是雜訊，是另一個狀態。暖機後同一組查詢中位數約 **210–220ms**。
>
> 因此 `verify_mv.py` 改成**明確量測暖機路徑**：每項先跑一次丟棄再計時，
> 並在輸出標明「冷啟動成本不在這裡」。冷啟動由 `app/mcp.warm_up()` 承擔——
> agent 啟動時先跑 `SELECT 1` ＋ 兩個 MV 輕量查詢（實測約 1.0–1.5s／次），
> 在模型介入之前吸收掉。
>
> M2 Phase A 不因此阻擋。demo 前若冷路徑仍會出現，選項是靠 warm-up 覆蓋
> （已實作）、前端顯示實際耗時而非承諾上限，或最後才考慮升級服務層級。

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

- [x] `app/prompts.py`：system instruction 含完整 DDL、view 用途、查詢範例與
      `QUERY_GUIDANCE`（AggregatingMergeTree `-Merge` 用法、先查廣再收窄、
      不掃 `film_attention`、interest 必須用 `interest_sample_count`）
- [x] `app/contracts.py`：Pydantic 契約；`PredictionScore` 的
      `commercial_score` / `attention_score` / `composite` 已允許 `None`，
      缺資料為 N/A，不再折成 0 分
- [x] `app/mcp.py`：`warm_up()` 走 MCP `run_query`，在 agent 前先跑
      `SELECT 1` ＋ 兩個 MV 輕量查詢，吸收 ClickHouse dev-tier 冷路徑
- [x] `app/guardrails.py`：查詢護欄落地，檢查 write attempt、舊欄位、
      未限制的 `film_attention` scan、interest 沒帶 `interest_sample_count`、
      nested `-Merge`、raw aggregate state；另用 `unsupported_terms()` 抓
      綜述中未被本輪查詢或結果支撐的受控詞彙
- [x] `app/agents/recombine.py`：`RecombineAgent` Phase A factory；
      tools 開啟、無 `response_schema`，只做自主查詢與 prose synthesis
- [x] `scripts/run_m2_recombine_phase_a.py`：M2 Phase A CLI；
      輸出 `docs/m2-recombine-phase-a-trace.log`，DoD 包含 warm-up、
      成功 response 扣掉 error、MV 查詢、guardrail、unsupported terms

**DoD**：agent 能自行決定查詢條件並取回聚合結果 → **PASS**。

驗收（2026-08-28）：

- `python3 -m pytest tests -q` → **44 passed**
- `python3 -m compileall app scripts etl tests` → PASS
- `./scripts/run_etl.sh scripts/verify_mv.py` → **16/16 PASS**；
  暖機後最慢中位數 **296.5ms**，最慢 tail **326.2ms**
- `./scripts/run_agent.sh scripts/run_m2_recombine_phase_a.py` → **PASS**；
  warm-up **3/3**，Gemini 自選 `run_query` **6 次**，成功 ClickHouse
  response **6 次**，**6/6** 查 MV，guardrail 違規 **0**、警告 **0**，
  unsupported terms **0**

> ⚠️ 提交前風險：`11479ac` commit message 含
> `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`。功能不受影響，
> 但與 submission checklist「commit 歷史無非 Google 的 AI 服務」相衝突；
> Devpost 前需處理。

### 8/29–8/30 週末（2h）

- [x] `RecombineAgent` Phase B grounded-only thin slice：
      關閉 tools，開 `output_schema=TreatmentProposal` 收斂為單一 grounded proposal
- [x] `app/proposal_validation.py`：Phase B 輸出驗收，檢查 schema parse、
      `variant=grounded`、evidence SQL 可回溯 Phase A trace、source_view 對得上查詢、
      樣本數不低於門檻、受控詞彙不憑空出現
- [x] `scripts/run_m2_recombine_phase_b.py`：讀
      `docs/m2-recombine-phase-a-trace.log`，不建立 MCP toolset，
      輸出 `docs/m2-recombine-phase-b-grounded-trace.log` 與
      `docs/m2-grounded-proposal.json`
- [x] 彩蛋分支：一次 `temperature=1.5` 額外呼叫（2026-08-29 完成，見 9/2 區塊）

**DoD（grounded-only thin slice）**：輸出一個 `TreatmentProposal`，且全程無 tool call
→ **PASS**。

驗收（2026-08-28，提前完成 grounded-only）：

- `python3 -m pytest tests -q` → **49 passed**
- `python3 -m compileall app scripts etl tests` → PASS
- `./scripts/run_agent.sh scripts/run_m2_recombine_phase_b.py` → **PASS**；
  tool call **0**、tool response **0**、`TreatmentProposal` parse **PASS**、
  `variant=grounded`、evidence grounding **PASS**

原週末完整 DoD（grounded ＋ wildcard 兩個 `TreatmentProposal`）已於 2026-08-29
隨 root 編排一併達成，驗收數據見 9/2 區塊。

### 8/31 一（3.5h）

> **下一個 agent session**：從此任務開始；部署決策已鎖定於 `docs/M4_DEPLOYMENT_PROMPT.md`。
> PredictAgent 詳細 task plan 見 `docs/M2_PREDICT_AGENT_PLAN.md`。

- [x] `AnalogueScoringRequest`（`app/contracts.py`）：proposal ＋ optional
      `budget_band` / `release_bucket`，預設 demo 用 mid-budget、先不套年份。
      `BUDGET_BANDS` 四段預算區間寫在 `app/config.py`，以 SQL predicate 形式
      進入 agent 指令，不用散文描述
- [x] `app/agents/predict.py`：`PredictAgent`（對外稱 **Analogue / Evidence Scoring Agent**）
      factory；stage 1 tools 開啟、無 `output_schema`，自主組裝類比查詢條件；
      stage 2 關 tools、`output_schema=AnalogueEvidenceBundle`
- [x] `scripts/run_m2_predict_agent.py`：讀 `docs/m2-grounded-proposal.json`，
      MCP `warm_up()` 後執行，輸出 `docs/m2-predict-agent-trace.log` 與
      `docs/m2-prediction-score.json`
- [x] SQL 錯誤與 guardrail failure 重試迴圈：`app/query_run.py` 記帳，
      連續失敗滿 `SQL_RETRY_LIMIT + 1 = 3` 次即停並輸出 `insufficient_evidence`；
      成功一次即歸零。護欄改為 `before_tool_callback`：違規查詢在送到
      ClickHouse **之前**被擋下，錯誤原文回給模型當作重試材料

**DoD**：trace 內可見 Gemini 自主查詢 `mv_motif_pair_stats`、
`mv_archetype_performance`、`films` 至少三個 surface，能取回同類歷史作品的
ROI 分位數與 sustained-interest proxy，且重試次數受控 → **PASS**。

### 9/1 二（3.5h）

- [x] Evidence extraction / validation（`app/analogue_scoring.py`）：
      `sql_query` 必須是本輪真的跑過的查詢；`source_view` 對得上；
      ROI 用 ROI count，interest 用 `interest_sample_count` /
      `countIf(has_interest_signal)`；**每個 `value` 與 `sample_count` 都必須
      出現在本輪某個 result payload 裡**
- [x] 可解釋評分邏輯：從通過驗證的 evidence 呼叫 `app/scoring.py`，
      得出 `commercial_score` ＋ `attention_score` → `composite`
- [x] `PredictionScore` 填充：輸出 **historical-analogue evidence**，
      每個數字附帶產生它的 SQL；模型寫的分數一律丟棄（收斂 schema
      根本沒有分數欄位，所以不是靠指令約束）
- [x] `insufficient_evidence` 分支（樣本數 < 8 或兩個維度都無 evidence，不硬猜）

**DoD**：評分附帶完整的歷史類比證據列表，而非黑箱票房預測數字 → **PASS**。

驗收（2026-08-29，提前完成）：

- `./scripts/run_etl.sh -m pytest tests -q` → **84 passed**
- `python3 -m compileall app scripts etl tests` → PASS
- `./scripts/run_agent.sh scripts/run_m2_predict_agent.py` → **11/11 PASS**：
  warm-up 3/3、11 次自主 tool call、三個 surface 全查到、0 錯誤 0 護欄攔截、
  收斂階段 0 tool call、evidence 驗證通過、9 筆過門檻 evidence、
  composite 由寫出的 JSON 重算一致（本輪 composite **62.33**）

**要記的三件事**：

1. **分數在跨次執行之間不是固定值。** 驗收跑出過 composite 60.36 / 59.07 /
   53.85 / 62.33，因為挑哪些類比集合是模型自己決定的，每次選的切片不同。
   這不是 bug，但 demo 不能講「這部片得 X 分」當成穩定事實；正確說法是
   「這一次檢索到的類比集合算出 X 分，證據就在旁邊」。要穩定就得把檢索策略
   固定成 query template（見 plan 的 kill criteria）。
2. **`PredictionScore.evidence` 型別從 `EvidenceItem` 改成 `AnalogueEvidence`。**
   Pydantic 依宣告型別序列化，用基底類別時 `metric` 欄位在寫檔時被丟掉，
   結果是「可重算的分數」寫出來的 JSON 反而無法重算——分不出哪筆餵
   commercial、哪筆餵 attention。runner 的重算檢查現在改成**讀回寫出的檔案**
   再算，而不是拿記憶體裡的 bundle 算，否則這個 bug 會一直是綠燈。
3. **`scripts/run_agent.sh` / `run_etl.sh` 加上 `--python-preference only-managed`。**
   原本只 unset `CONDA_PREFIX` 不夠：uv 仍會從 PATH 挑到 miniconda 的 python，
   而當那個 interpreter 已滿足所有 `--with` 條件時，uv 直接在那裡跑、不建 overlay。
   miniconda base 有 google-adk 2.7.1 ＋ mcp 1.15.0，`mcp<2` 被 1.15.0 滿足，
   於是 ADK 在 `from mcp import SamplingCapability` 掛掉。版本條件有滿足，
   組合仍然是壞的。

### 9/2 三（3.5h）

- [x] Root Agent 編排（`app/pipeline.py`）
- [x] 端到端 CLI 跑通（`scripts/run_greenlight.py`）
- [x] 結構化 log 輸出（`docs/m2-greenlight-events.jsonl`，走 `InProcessEventBus`）
- [x] 彩蛋分支：`temperature=1.5` 的 wildcard（原列在 8/29–8/30，一併完成）
- [x] Phase A handoff 檢查：grounded proposal 之前必須有
      `mv_motif_pair_stats` 和 `mv_archetype_performance` 的成功結果；缺 surface
      時由 RecombineAgent 補查後再進 Phase B
- [x] PredictAgent 查詢粒度收斂：motif pairs / archetypes 優先用 set-based SQL
      一次查同一個 surface，避免 wildcard 分支展開成多個同質平行查詢

**DoD**：一次執行輸出雙方案 JSON ＋ 完整 tool call trace → **PASS**。

驗收（2026-08-29，提前完成）：

- `./scripts/run_etl.sh -m pytest tests -q` → **88 passed**
- `python3 -m compileall app scripts etl tests` → PASS
- `./scripts/run_agent.sh scripts/run_greenlight.py` → **13/13 PASS**：
  warm-up 3/3、Phase A 6 次自主查詢、handoff surface 完整、兩個方案都通過
  驗證與評分、22/22 個 `tool_call` 事件都帶 SQL 原文、SSE 需要的事件型別
  齊全、狀態機停在 `awaiting_approval`、總計 22 次 tool call / 0 SQL 錯誤 /
  0 護欄攔截 / 250.9s；本輪 composite：grounded **64.49**、wildcard **49.85**

**沒有用 `SequentialAgent`，理由要記下來**：`SequentialAgent` 把 sub-agent 串在
同一個 invocation 和同一個 session 裡。這條 pipeline 不是「一串模型呼叫」，是
「模型呼叫中間夾決策」——Phase B 的輸出要先 parse 和驗證才准往下用；一個 variant
失敗不該中止整條流程；分數由 `app/scoring.py` 算，根本不是 agent 能當的一步。
ADK 對這種形狀的官方答案是自訂 `BaseAgent` 而不是 `SequentialAgent`，但自訂
`BaseAgent` 會把 sub-agent 放回同一個 session——那正是 Phase B 不能有的東西：
Phase B 沒有 tools，它必須把 Phase A trace 當成「引用的資料」，不是「自己可以續寫
的對話歷史」。給它那段歷史，就是一個「無工具」階段開始描述自己跑過的查詢的起點。
所以每個 stage 各自 `Runner` ＋ 各自 session，交接用明確的 transcript；事件照樣
全部進同一條 bus，SSE 看到的仍是一條連續的 run。

**三個實測發現**：

1. **彩蛋方案方向正確但區隔力偏弱。** grounded 56.7 / wildcard 47.9，差 8.8 分。
   方向對（資料不支持的組合分數較低），但兩邊 confidence 都是 high，說服力有限。
   原因看得出來：PredictAgent 的 film-level 查詢用寬鬆交集（motif 至少中一個
   **OR**、archetype 至少中一個），而 wildcard 挑了 4 motif ＋ 3 archetype、
   grounded 只有 2 ＋ 2——OR 的項越多，類比集合越大越通用，分數越往全體中位數回歸。
   要真正拉開區隔得把項數正規化或改交集權重，那會動到評分語意，賽前不動。
   **值得記的是 wildcard 的 proposal evidence 是 0 筆**——它照指示誠實回報
   「transcript 沒量過這個組合」，而不是硬湊一筆支持證據。
2. **`response_schema` 不保證 `maxLength`。** Gemini 會遵守 shape 和 enum，
   但不遵守字串長度上限：有一輪 logline 寫了 214 字元，回來是合法 JSON，
   然後被 pydantic 擋掉，整個 grounded variant 就沒了，而 wildcard 在旁邊跑完。
   Phase B 現在有重試（和 SQL 同一個預算 `SQL_RETRY_LIMIT + 1`），
   而且把 validation error 原文回給模型——講「logline 最多 200 字元」它會改，
   只講「再試一次」它會寫出另一個一樣過長的。
3. **不要叫模型抄 SQL。** 收斂階段原本要求把查詢逐字複製進 `sql_query`，
   多數時候會照做，但有一輪它把 `budget_usd >= 20000000 AND budget_usd < 80000000`
   自己加了一層括號——語意相同、字面不同，grounding 檢查直接拒絕，
   一筆背後有 133 部片的證據就這樣掉成 `insufficient_evidence`。
   現在 transcript 幫每個查詢編號，模型只回報 `query_index`，SQL 由 Python 從
   `QueryRun.queries` 貼回去，`source_view` 也由 SQL 推導。
   模型沒打過的字串，它就沒辦法改寫。

**順帶記一筆**：這輪 grounded 方案的 archetype 用到 `reluctant_hero`，
就是那個同時存在於 motif 和 archetype 的 P1 詞彙重疊。用在這裡語意沒問題，
但它確實會出現在 demo 產物裡，賽後清詞彙邊界時記得。

### ⛔ Kill Criteria

**9/2 晚端到端未通 → 砍彩蛋方案，只保留單一方案路徑。**

---

## M3 — 媒體與前端

> 9/3–9/7，12.5 小時
> **目前分支**：`feature/m3-media-frontend-plan`
> **執行計劃**：`docs/M3_MEDIA_FRONTEND_PLAN.md`
> **下一個 coding task**：M3 Task 3 — browser experience / Ken Burns player
> （Task 0 API/SSE shell、Task 1 StoryboardPlan、Task 2 media code path 已於
> 2026-08-29 完成；Task 2 付費 real smoke 尚未執行）。

M3 的順序已於 2026-08-29 調整：先做公開 demo 的 API/SSE shell 與成本保護，
再做 Imagen/TTS 媒體。原因是 Storyboard 可以降級，但評審看得到 SQL trace、
雙方案、approve gate 這條主路徑不能缺。

### 9/3 四（3.5h）

- [x] `app/main.py`：FastAPI endpoints
      `GET /`、`POST /run`、`GET /events/{run_id}`、
      `POST /approve/{run_id}`、`GET /runs/{run_id}`、`GET /health`、`GET /ready`
- [x] API path 接上既有 `run_greenlight()`；`run_greenlight()` 改發
      `awaiting_approval`（非終止），`done` 由呼叫端決定何時發
- [x] 公開 demo 保護：`ANALYSIS_SLOT` 一次一個分析、每 IP 10 分鐘 3 次、
      prompt 400 字上限、409 busy response
- [x] `InProcessEventBus` replay hardening：history cap、QueueFull 安全、
      `close()` 保留 history、`discard()` 與 `RunStore.sweep_ids()` 成對回收
- [x] `web/index.html` 最小骨架：啟動 run、接 SSE、顯示 SQL/rows/latency、
      顯示 proposals/scores、送 approve
- [x] `scripts/serve.sh`：本機服務進入點（和 agent 同一套環境衛生）

**DoD**：瀏覽器能看到 M2 agent 查詢過程，analysis 完成後停在 approve gate；
unit tests 不打 Gemini / ClickHouse / Imagen → **PASS**。

驗收（2026-08-29，提前完成）：

- `./scripts/run_etl.sh -m pytest tests -q` → **109 passed**（新增 21 個：
  `tests/test_events.py` 8、`tests/test_api.py` 13），全部用 fake analysis，
  不打任何付費 API，0.31s 跑完
- `python3 -m compileall app scripts etl tests` → PASS
- 真實伺服器 smoke：`PORT=8099 ./scripts/serve.sh` → `/health`、`/`、`/ready` 皆正常
- **真實端到端（走 API，不是 CLI）**：`POST /run` → SSE 41 個事件依序抵達 →
  `awaiting_approval` → `POST /approve` → `done`。
  grounded `The Unveiling` composite 58.6（high, 12 evidence）、
  wildcard `The Obsidian Vault` composite 42.3（high, 7 evidence）

**開工前查出的三件 plan 沒涵蓋的事**：

1. **`error` 事件會關掉 SSE 訂閱，而 pipeline 拿它報可復原的狀況。**
   `app/events.py` 的 `subscribe()` 收到 `done` 或 `error` 就 return，
   而 `app/pipeline.py` 有四處發 `error`：Phase B 某次嘗試被拒（**馬上重試**）、
   stage 撞到 retry/turn 上限、一個 variant 失敗（另一個還在跑）、stage 例外。
   也就是說上一輪那個 214 字元 logline 被拒然後重試成功的情況，
   **在瀏覽器裡會表現成「串流無聲無息停住」**——CLI 一路跑完，SSE 訂閱者
   在第一次重試就被踢掉。CLI 不訂閱自己的 bus，所以 M2 的 12/12 完全照不到。
   現在事件分三類：`agent_retry`（還會再試）、`stage_failed`（這一段放棄，
   run 繼續）、`error`（run 結束）。`TERMINAL_EVENTS` 是具名常數，
   `tests/test_events.py` 直接斷言前兩者不在裡面。
   **這輪真實 API run 就驗到了**：中途有一次真的 ClickHouse code 47，
   串流照樣往下跑了 8 次 tool call 到 `awaiting_approval`。
2. **核准閘門原本會把唯一 `/run` 名額無限期佔住。** 決策：名額只護分析階段，
   proposals 一產生就釋放；媒體另用 `MEDIA_SLOT`；停在閘門的 run 設 10 分鐘
   `APPROVAL_TTL_SEC` 只為記憶體回收。這樣一個評審看完方案就去吃飯，
   不會讓後面每個訪客都拿到 busy。`RunStore` 因此要能同時持有多個等核准的 run。
3. **`/health` 不能做 MCP warm-up。** plan Task 4 原本要 `/health` 跑
   `SELECT 1`，但實測 `/ready` 的 connectivity 冷啟動是 **25.4 秒**
   （另兩個 MV 各約 0.96 秒）。startup probe 等不了，會殺掉容器再重啟進同一個
   冷啟動，無窮迴圈。已拆成便宜的 `/health`（liveness）與 `/ready`（做 warm-up）。

**另外兩個順手修掉的**：`subscribe()` 的 replay 對 bounded queue 逐筆
`put_nowait`，history 超過 `MAX_QUEUED` 就把 `QueueFull` 丟在讀者身上；
`close()` 原本直接丟掉 history，導致 run 結束後重新整理頁面看不到 trace，
現在保留 history 並在 sweep 時才 `discard()`。

**驗收補修**：API `_analyse()` 會暫存 pipeline 發出的 `awaiting_approval`
事件，等 proposals/scores 寫進 `RunStore` 並切到 `AWAITING_APPROVAL` 後才
發布 SSE。這避免很快的前端收到 gate 事件後立刻打 `/approve` 時，狀態機仍是
`running` 而偶發 409。

### 9/4 五（3.5h）

- [x] `app/agents/storyboard.py`：approved proposal → exactly 3 scene plans
      （M3 Task 1，2026-08-29 完成）
- [x] `app/media.py` 前半：`StoryboardPlan` 驗證、`HOUSE_STYLE`、prompt 組裝、
      時長估算——**不花錢就能查的部分**
- [x] `app/media.py` 後半：Imagen 生圖，輸出 16:9 scene still
- [x] GCS 上傳與 URL 產生（signed URL；可用 `GCS_PUBLIC_ASSETS=true`
      明確切 public demo bucket）
- [x] Cloud TTS 旁白（Chirp 3 HD，LINEAR16/WAV，從 WAV header 算實際時長）
- [x] `SceneAsset` 契約填充完成；媒體錯誤會 publish terminal `error` event
- [x] `/approve` 改成背景 task：同步只切到 `STORYBOARD` 並排入
      `_render_approved_variant()`；`MEDIA_SLOT` 保護 StoryboardAgent +
      Imagen/TTS/GCS；`media_ready` 等 `run.scenes` 寫入後才發布
- [ ] Task 2 付費 real smoke：
      `./scripts/run_agent.sh scripts/run_m3_media.py --yes`

**DoD**：3 張風格一致的 16:9 圖片可由 URL 存取；音訊可播放或明確走
Google-only fallback。

#### Task 2 mock 驗收（2026-08-29，提前完成）

- `./scripts/run_etl.sh -m pytest tests -q` → **141 passed**（不打 Gemini /
  ClickHouse / Imagen / TTS / GCS）
- `python3 -m compileall app scripts etl tests` → PASS
- `git diff --check` → PASS
- `PORT=8099 ./scripts/serve.sh` smoke → `/health` PASS、`/` 200、
  `/ready` 3/3 PASS；這次 connectivity 冷路徑 **23.2s**，
  再次確認 `/ready` 不能放 startup/liveness probe
- `./scripts/run_agent.sh scripts/run_m3_media.py` → DRY RUN PASS，只讀
  `docs/m3-storyboard-plan.json` 並列出 composed Imagen prompts；不帶 `--yes`
  不呼叫 Imagen / TTS / GCS
- real smoke 入口已加：`scripts/run_m3_media.py --yes`。輸出的
  `docs/m3-media-assets.local.json` / trace 被 `.gitignore` 保護，避免
  signed URL 誤 commit。

#### Task 1 驗收（2026-08-29，提前完成）

- `./scripts/run_etl.sh -m pytest tests -q` → **130 passed**（`tests/test_media.py` 新增 21）
- `./scripts/run_agent.sh scripts/run_m3_storyboard.py` → **9/9 PASS**，
  輸出 `docs/m3-storyboard-plan.json` 與 trace

**為什麼 plan 是獨立產物**：媒體是整條 pipeline 唯一不可逆的花費。核准閘門之前
的一切都能用幾個 ClickHouse 查詢重跑，三張 Imagen ＋ 三段 TTS 不行。所以先產出
一個**不花錢就能驗**的 `StoryboardPlan`，再拿它去生媒體。要防的不是醜圖，
是**三張很好但屬於另一部片的圖**——所以驗證項目是 title / variant / 詞彙沒有漂移，
而不是美學。

`converge_with_retry()` 從 `recombine_phase_b` 抽出來共用：兩者都是「無 tools ＋
output_schema ＋ 被拒就帶著錯誤原文重試」，重試預算和 SQL 一樣是
`SQL_RETRY_LIMIT + 1`。

**第一次真實跑就抓到三個問題**（DoD 擋下來的，不是事後發現的）：

1. **negation-blind 的字幕偵測誤判自己。** `HOUSE_STYLE` 本身寫著
   「No text, no captions, no logos, no watermarks」，而驗收腳本拿**組合後**的
   prompt 去查，於是三個場景全被標成「要求畫面內文字」。兩處都錯：腳本應該查
   模型自己寫的 `image_prompt`，而 `lettering_requests()` 應該忽略被否定的提及。
   否定只涵蓋自己的子句——否則 `no watermark, but a title card reading THE END`
   會因為那個 `no` 還在回看窗內而整句被當成否定。
2. **風格被講了三次。** 模型把 `style` 幾乎逐字設成 house style，
   而場景 prompt 又各自重述一次景深/顆粒/色調，組合後同一組底片詞彙在主體出現
   之前重複三輪。加了 `restates_house_style()` 檢查（內容詞重疊 ≥ 70% 即判定），
   指令也改成「style 要寫這部片自己的樣子」。重跑後模型給的是
   「Gritty urban realism, rain-slicked streets, institutional interiors...」。
3. **prompt 順序放反了。** 原本是 house style 開頭，等於在畫面主體前面塞四個
   子句的底片術語。Imagen 讀自然語言、開頭權重最高，改成
   **moment → 這部片的 style → house style**。

**已在 Task 2 修掉**：`/approve` 不再同步走完 `STORYBOARD → DONE` 並
`bus.close()`。現在 approve 只排背景 task；`done` 與 `close()` 在媒體完成後才發，
`media_ready` 則等 `run.scenes` 寫進 `RunStore` 後發布。

### 9/5–9/6 週末（2h）

- [x] 雙方案對比區塊 ＋ 核准按鈕
- [x] **證據流區塊：顯示 SQL 原文、列數、耗時、錯誤與 retry**
- [x] CSS Ken Burns 播放器（`transform: scale` ＋ `translate`，
      `audio` 結束事件切換場景，無音訊則走 `duration_sec` timer）
- [x] Mobile / desktop layout 檢查，避免文字或按鈕重疊

**DoD**：瀏覽器可完整跑 run → SSE evidence → proposal compare → approve →
storyboard playback → **PASS**。

#### Task 2 ＋ Task 3 驗收（2026-08-30）

- `./scripts/run_etl.sh -m pytest tests -q` → **141 passed**
- `./scripts/run_agent.sh scripts/run_m3_media.py --yes` → **4/4 PASS**
- **真實端到端走 API**：`POST /run` → 68 個 SSE 事件 → `awaiting_approval`
  → `POST /approve` → `media_ready` → `done`；事件順序與前端讀的每個欄位
  都以程式檢查過（`tool_call.args.query`、`tool_result.rows/elapsed_ms`、
  proposal/score/evidence/scene 欄位、`gate → media_ready → done` 排序）
- 資產以匿名 `curl` 驗證：3 張 1344×768（16:9）PNG ＋ 3 段 24 kHz WAV，
  全部 HTTP 200、content-type 正確、時長 8.5 / 11.1 / 8.2 秒（從音檔讀出來的，
  不是字數估的）
- 前端以 headless Chromium 在 1440×1000 與 390×844 兩個尺寸截圖檢查：
  **0 個版面問題、0 個 JS error**，11 種 SSE 事件型別全部有處理

**Task 2：plan 指定的兩個 Google 服務，這個專案一個都用不了**

- Imagen 全系列（`imagen-4.0-generate-001`、fast 版、`imagen-3.0-generate-002`）
  一律 `404 not found or your project does not have access`
- Cloud Text-to-Speech API 沒啟用，而且**沒辦法從這裡啟用**——Service Usage API
  本身也是關的，「用來開 API 的那個 API」是關的

改用同一個 Vertex surface（agent 已在用、不需要多開任何 API，仍然 Google-only）：
`gemini-2.5-flash-image` ＋ `gemini-2.5-flash-preview-tts`。TTS 回 raw PCM，
用 stdlib `wave` 包成 WAV——不是為了省一個轉檔器，是因為這樣
`wav_duration_sec()` 就變成**實測**而不是字數估算的 fallback。
既有的 `MediaClient` protocol、signed URL 路徑、循序生成全部保留，只換兩個 adapter。

**然後第一次真的生成被整個擋掉**：

```
block_reason=SAFETY
'The prompt is blocked due to requesting to remove watermarks'
```

house style 結尾的 `no logos, no watermarks` 被分類器讀成「要求移除浮水印」。
但那句不能拿掉——沒有它的探測圖右下角就烙著 `02:47 AM` 和一個膠卷圖示。
改成 `Clean frame with no lettering, captions or on-screen graphics.`，
意圖不變、不踩觸發詞。**負面提示的措辭本身會決定請求會不會被拒。**

資產走公開 bucket（`greenlight-agent-demo`）。signed URL 需要 service account，
而本機的 user ADC 簽不了名——那會變成「本機可以、Cloud Run 可以，但兩邊行為不同」，
留到登場前才發現。

**Task 3：截圖檢查抓到兩個「測試看不到」的問題**

1. **storyboard 在按 Play 之前是一塊純黑**。技術上正確（還沒開始播），
   但讀起來像圖片載入失敗。改成建好就顯示第一格靜態畫面，
   Ken Burns 動畫改由 `.playing` class 觸發，播完停在最後一格而不是淡回黑。
2. **證據流被 schema 階段的 JSON 洗版**。Phase B / converge 的模型輸出就是
   一整包 JSON，`agent_output` 原樣印出來會把這個面板存在的理由（SQL）擠掉。
   結構化輸出現在收合成一行摘要，散文照原樣顯示。

### 9/7 一（3.5h）

- [x] Dockerfile ＋ `.dockerignore`（**Phase 1 基礎版**：單容器 stdio MCP、
      雙 venv 隔離、非 root、預裝 mcp-clickhouse）
- [x] README 更新：實際部署架構、Google-only AI 服務、ClickHouse/MCP runtime path、
      score 不是票房預測的邊界聲明、部署指令
- [x] **容器內完整跑通一次**（等同無痕：容器沒有 `.env`、沒有 repo、
      只有 image ＋ 注入的環境變數）
- [ ] `gcloud run deploy` 實際部署 → **卡住，需要你操作**（見下）
- [ ] Secret Manager 建立兩個 secret
- [ ] 公開 URL 無痕視窗驗收

**DoD**：公開 URL 可用，陌生人能自行跑完一次 → **容器層已通過，雲端部署待你授權**。

#### Task 4 驗收（2026-08-30）

- `./scripts/run_etl.sh -m pytest tests -q` → **145 passed**
- `docker build` → 406 MB，非 root（uid 1001），`/opt/mcp-env` 與 `/opt/app-env`
  互不可見
- 容器內 `/health` → ok；`/ready` → 3/3，1261 / 970 / 957 ms
- **容器內完整端到端**：`POST /run` → SSE → `awaiting_approval` → `POST /approve`
  → `media_ready` → `done`；事件契約檢查 PASS；6 個資產匿名 `curl` 全部 HTTP 200

**`app/mcp.py` 加了 `MCP_SERVER_CMD`**。開發路徑是 `uv run --with mcp-clickhouse`，
會在 agent 第一次需要時才下載套件；那在容器裡等於把一次套件下載放在冷啟動路徑上，
而這個 demo 光是等 ClickHouse 醒來就已經要 25 秒。image 改成 build 時裝進
`/opt/mcp-env`，`MCP_SERVER_CMD` 指向那個 binary。

**第一次容器跑通分析、卻在媒體掛掉**，而且掛得很有代表性：

```
ClientError: 429 RESOURCE_EXHAUSTED
```

分析已經付完錢、閘門也已經過了，才在最後一步倒下——這是最糟的時機。
`app/media.py` 現在對**暫時性**錯誤（429 / 503 / 500 / deadline / timeout）
做 4 次指數退避重試，對**拒絕**（safety block、404）不重試——被擋的 prompt
問幾次都是同一個答案，重試只是拿 demo 的時間去換一樣的結果。

重建後再跑一次，重試真的觸發了兩次並且完成：

```
image: ClientError on attempt 1/4, retrying in 4s
image: ClientError on attempt 2/4, retrying in 8s
```

**卡住的地方：我無法實際部署。** `gcloud` CLI 沒有登入帳號
（`You do not currently have an active account selected`），而且這個專案的
Service Usage API 是關的——Artifact Registry / Cloud Build / Cloud Run
很可能也需要先啟用。這件事**現在就要處理**，不要留到 9/7。

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
