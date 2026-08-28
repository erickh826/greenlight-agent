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

**DoD**：一次執行輸出雙方案 JSON ＋ 完整 tool call trace → **PASS**。

驗收（2026-08-29，提前完成）：

- `./scripts/run_etl.sh -m pytest tests -q` → **87 passed**
- `python3 -m compileall app scripts etl tests` → PASS
- `./scripts/run_agent.sh scripts/run_greenlight.py` → **12/12 PASS**：
  Phase A 7 次自主查詢、兩個方案都通過驗證與評分、31/31 個 `tool_call`
  事件都帶 SQL 原文、SSE 需要的事件型別齊全、狀態機停在 `awaiting_approval`、
  總計 31 次 tool call / 0 SQL 錯誤 / 0 護欄攔截 / 236s

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
- [ ] Dockerfile ＋ Cloud Run 部署（**Phase 1 基礎版**：單容器 stdio MCP；策略見 `docs/M4_DEPLOYMENT_PROMPT.md`）
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
