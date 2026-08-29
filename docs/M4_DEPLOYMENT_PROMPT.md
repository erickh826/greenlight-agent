# Agent Prompt: Cloud Run 部署策略（Phase 1 基礎版 → Phase 2 Sidecar）

**用途**：給 coding agent 或下一個 session 的執行指令。  
**前置**：M1 資料層已載入 ClickHouse；M2 Phase A/B（grounded）已通過。  
**對照**：`suggestion/CLOUD_RUN_DEPLOYMENT_PLAN.md`（Phase 1 細節）、`SYSTEM_SPEC §6`、`app/mcp.py`、`app/state.py`、`app/events.py`。

---

## 你的角色

你是 Greenlight Agent 的部署工程師。目標是在 **9/7 凍結前** 讓公開 hosted URL 穩定跑通完整 demo，並在時間允許時將 MCP 升級為 Cloud Run Sidecar。**穩定優先於架構炫技。**

---

## 不可違反的約束（違反即失格或 demo 崩潰）

1. **Runtime 必須經 `mcp-clickhouse` 查 ClickHouse** — 評審要看 `run_query` 的 SQL trace。
2. **AI 服務僅限 Google Cloud** — 無 OpenAI / Anthropic / 其他 vendor。
3. **HITL 不得引入分散式佇列** — 本競賽階段禁止 Cloud Workflows / Redis / Pub/Sub 做 approve 同步。
4. **單實例部署** — `--min-instances=1 --max-instances=1`，直到刻意做多實例壓測。
5. **憑證不進 repo** — Secret Manager 注入；提交前掃 `git log -p`。
6. **9/7 晚凍結** — Phase 2 Sidecar 是加分項，**不能阻擋** Phase 1 無痕視窗驗收。

---

## 架構判斷（已決策，勿重新辯論）

### Goldilocks 原則

| 要做 | 不做（本階段） |
|---|---|
| 單 Cloud Run 實例 + 進程內 `RunStore` / `InProcessEventBus` | 分散式 HITL（Workflows callback） |
| `asyncio.create_task` 跑 Storyboard（Imagen/TTS） | Cloud Tasks 排隊生圖 |
| `no-cpu-throttling` 保背景算力 | Redis Pub/Sub 跨實例 SSE |
| MCP 與 ADK **環境隔離**（先 stdio/雙 venv，後 Sidecar） | 為答辯提前上 multi-region |
| MV 預聚合 + agent 查 MV | 加 secondary index / 向量檢索 |

### 為什麼 HITL 保持進程內

`/approve` 依賴 `RunStore` 狀態機 + `InProcessEventBus`。兩者都在**單一容器記憶體**中。  
換成分散式佇列不會讓 demo 更穩，只會讓「SSE 掛住但無錯誤」的除錯難度倍增。  
**Sidecar 只換 MCP transport，不動 HITL。**

### 兩階段 MCP 策略

```
Phase 1（必做，9/7 前）
  FastAPI + ADK ──stdio──> mcp-clickhouse subprocess
  單容器，/opt/mcp-env 雙 Python 環境（或 uv run 隔離）
  M0/M2 已驗證此路徑

Phase 2（加分，9/7 凍結後或 M3 尾聲有空）
  [主容器 :8080] ──HTTP MCP──> [Sidecar :8000]
  mcp-clickhouse: CLICKHOUSE_MCP_SERVER_TRANSPORT=http
  主容器 app/mcp.py 改 HTTP client + Bearer token
  Cloud Run multi-container revision
```

**類比（答辯用）**：GCP Cloud Run Sidecar ≈ AWS ECS multi-container ≈ Azure ACA multi-container。

---

## Phase 1：基礎版實作指令

### 架構圖（對外/README 用此版，直到 Phase 2 完成）

```
[Browser]
   │  GET /events/{id}  (SSE)
   │  POST /approve/{id}
   ▼
┌─ Cloud Run (min=1, max=1, cpu-always-on, timeout=900) ─────────────┐
│  FastAPI + ADK Root Agent                                          │
│    ├─ RunStore (in-memory state machine)                           │
│    ├─ InProcessEventBus (SSE fan-out)                              │
│    ├─ MCPToolset ──stdio──> mcp-clickhouse (/opt/mcp-env)          │
│    └─ asyncio.create_task ──> StoryboardAgent (Imagen/TTS → GCS)   │
└──────────────┬─────────────────────────────┬───────────────────────┘
               ▼                             ▼
      ClickHouse Cloud                 Vertex AI + GCS
```

### Cloud Run 部署參數（Phase 1 固定）

```bash
--min-instances=1
--max-instances=1
--no-cpu-throttling
--timeout=900
--concurrency=10   # 配合 Semaphore，同時只允許 1 active analysis
```

### 必做檔案（Phase 1 DoD）

| 檔案 | 內容 |
|---|---|
| `app/main.py` | FastAPI：`POST /run`、`GET /events/{id}`、`POST /approve/{id}`、`GET /health`、`GET /ready` |
| `web/index.html` | SSE 證據流 + 雙方案對比 + approve + Ken Burns 播放器 |
| `app/media.py` | Imagen + TTS + GCS upload |
| `Dockerfile` | 雙 venv MCP 隔離；非 root；預裝 mcp-clickhouse |
| Secret Manager | `CLICKHOUSE_HOST`、`CLICKHOUSE_PASSWORD` |

### `/health` / `/ready` 必須分開

`/health` 是便宜 liveness only：只確認 FastAPI 活著，不碰 MCP / ClickHouse /
GCS。Cloud Run startup/liveness probe 只能打這個端點。

`/ready` 才做 MCP warm-up：`SELECT 1` ＋ MV 輕量查詢，走 `warm_up()` 同路徑並
回報耗時。demo 前人工打 `/ready`；不要放進 startup probe，ClickHouse Cloud
冷路徑實測可到 25 秒以上。

### SSE 標頭（不可省略）

```
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

### 公開 demo 保護（M3 P0，比 Sidecar 優先）

- `ANALYSIS_SLOT`：同時只有 1 個 active analysis；`awaiting_approval` 不佔名額
- `MEDIA_SLOT`：核准後的 storyboard / media 另排隊
- `/run` rate limit（每 IP 每分鐘 1 次）  
- Storyboard `create_task` 例外必須 `event_bus.publish({"type": "error", ...})`

### Phase 1 驗收（9/7 DoD）

- [ ] 無痕視窗完整跑通：run → SSE 顯示 SQL → 雙方案 → approve → 分鏡播放  
- [ ] `./scripts/test_mcp_clickhouse.sh` 在容器外仍 PASS  
- [ ] `python3 -m pytest tests -q` PASS  
- [ ] hosted URL 可從 Devpost 表單填寫  

---

## Phase 2：Sidecar 升級指令（僅在 Phase 1 穩定後）

### 前置驗證（改 Cloud Run yaml 之前，本機完成）

```bash
# Terminal 1 — MCP HTTP server
export CLICKHOUSE_MCP_SERVER_TRANSPORT=http
export CLICKHOUSE_MCP_BIND_HOST=127.0.0.1
export CLICKHOUSE_MCP_BIND_PORT=8000
export CLICKHOUSE_MCP_AUTH_TOKEN="$(uuidgen)"
# + 既有 CLICKHOUSE_HOST / USER / PASSWORD
mcp-clickhouse   # 或 /opt/mcp-env/bin/mcp-clickhouse

# Terminal 2 — 驗證 ADK HTTP client（改 app/mcp.py 後）
MCP_TRANSPORT=http MCP_URL=http://127.0.0.1:8000 \
  ./scripts/run_agent.sh scripts/run_m2_recombine_phase_a.py
```

**DoD**：`list_databases` + `run_query` 與 stdio 版行為一致。

### Sidecar 容器 env

```bash
CLICKHOUSE_MCP_SERVER_TRANSPORT=http
CLICKHOUSE_MCP_BIND_HOST=127.0.0.1
CLICKHOUSE_MCP_BIND_PORT=8000
CLICKHOUSE_MCP_AUTH_TOKEN=<secret>
CLICKHOUSE_HOST=...
CLICKHOUSE_PASSWORD=...
```

### 主容器 env（Sidecar 後可移除 ClickHouse 密碼）

```bash
MCP_TRANSPORT=http
MCP_URL=http://127.0.0.1:8000
MCP_AUTH_TOKEN=<same secret>
```

### `app/mcp.py` 改動原則

- 支援 `MCP_TRANSPORT=stdio|http` 環境變數切換  
- **只改此檔** + deploy yaml + Dockerfile；`RunStore` / `EventBus` / agents 不動  
- 主容器 `warm_up()` 對 MCP 加 retry（3 次 × 2s），因 sidecar 與主容器同時啟動  

### Phase 2 驗收

- [ ] 無痕視窗 demo 與 Phase 1 同等穩定  
- [ ] README 架構圖更新為 Sidecar 版  
- [ ] 能一句話解釋：「Agent 與 MCP 以獨立容器、localhost HTTP 解耦」  

### Phase 2 Kill Criteria

若本機 HTTP MCP 與 ADK 整合超過 **4 小時**仍失敗 → **放棄 Sidecar**，Phase 1 照常提交。

---

## 答辯話術（依階段選用）

**Phase 1（stdio / 進程隔離）：**
> Runtime 透過 MCP 協議連接 ClickHouse；Agent 與 MCP server 以獨立 Python 環境隔離。展示部署鎖定單一 Cloud Run 實例，進程內 EventBus 保證 SSE 證據流與核准閘門一致；媒體生成以 asyncio 非同步執行，配合 always-on CPU。

**Phase 2（Sidecar 完成後）：**
> 我們將 MCP 提升為 Cloud Run Sidecar，以 HTTP transport 在本機網路解耦 Agent 與資料工具層。HITL 狀態機仍在主容器；生產擴展時 EventBus 可替換為 Memorystore，長任務可下沉 Cloud Tasks — 介面已預留 Protocol。

**不要說**「我們已接入 Redis / Cloud Tasks」— 除非真的做了。

---

## 下一步執行鏈（從當前 milestone 接上）

> 當前位置：M3 已完成並部署。公開 URL 已跑過真實瀏覽器無痕驗收：
> run → SSE SQL evidence → proposal compare → approve → media_ready → done。
> 下一個阻斷點是 M4 提交：錄影、README 最終核對、全歷史掃密、Devpost。

### M4 前最後部署核對

```bash
# 1. 本機最後驗收
python3 -m pytest tests -q
python3 -m compileall app scripts etl tests

# 2. 若有代碼變更才重建 + 部署
gcloud builds submit --tag ${IMAGE_TAG} .
gcloud run deploy greenlight-agent \
  --min-instances=1 --max-instances=1 \
  --no-cpu-throttling --timeout=900 \
  ...

# 3. 暖機 + 無痕視窗完整 demo
curl ${SERVICE_URL}/health
curl ${SERVICE_URL}/ready
# 瀏覽器：run → SSE → approve → storyboard
```

### M4（9/8–9/9）— 提交；Sidecar 僅為加分

| 優先 | 任務 |
|---|---|
| P0 | 錄影、YouTube 公開、README 架構圖（用 Phase 1 或 Phase 2 圖，標註實際狀態） |
| P0 | `git log` secret scan、Devpost 提交 |
| P1 | Phase 2 Sidecar（**僅當 9/7 Phase 1 已穩定且 9/8 上午有空**） |

---

## Agent 執行時的決策樹

```
收到部署相關任務
  │
  ├─ 今天 < 9/7 且 Phase 1 未通無痕測試？
  │     └─ YES → 只做 Phase 1；拒絕 Sidecar / Redis / Workflows
  │
  ├─ 任務是否涉及 HITL / approve？
  │     └─ YES → 只用 RunStore.transition()；禁止外部佇列
  │
  ├─ 任務是否涉及 MCP？
  │     ├─ Phase 1 未穩定 → stdio + 雙 venv
  │     └─ Phase 1 已穩定 + 本機 HTTP MCP 已驗證 → Sidecar yaml
  │
  └─ 任務是否涉及查詢效能？
        └─ 改 MV / prompts / warm_up()；不加 index / thread pool
```

---

## 參考檔案

| 檔案 | 用途 |
|---|---|
| `app/mcp.py` | MCP 建構與 warm_up；Sidecar 時只改這裡 |
| `app/state.py` | HITL 狀態機；Sidecar 不動 |
| `app/events.py` | SSE bus；Sidecar 不動 |
| `suggestion/CLOUD_RUN_DEPLOYMENT_PLAN.md` | Phase 1 Dockerfile / gcloud 指令 |
| `MILESTONES.md` §M3 9/7、§M4 | 日期與 DoD |
| `scripts/verify_mv.py` | 查詢 latency 基準（中位數 < 500ms） |

---

*最後更新：2026-08-28。架構決策：先通基礎版，再衝 Sidecar。*
