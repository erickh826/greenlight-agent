# M0 手動步驟清單（8/20）

本文件記錄無法由 agent 代為完成的步驟。本地檔案與腳本已就緒。

## 1. Google Cloud $100 credits 申請

- **表單**：https://forms.gle/XPe837tzogh8L5sX6
- **硬截止**：2026-08-31 23:59 PST
- **核發時間**：1–5 工作天
- **備選**：https://cloud.google.com/free 免費試用（$300 credits）

> 今天（8/20）就填，避免 8/31 截止前來不及核發。

## 2. ClickHouse Cloud 開通

- **註冊**：https://console.clickhouse.cloud/signUp
- **試用**：$300 credits / 30 天
- **建議設定**：
  - Cloud provider: GCP（與 hackathon 一致）
  - Tier: Development（省 credits）
  - Region: 選離你最近的

開通後到 **Connect** 分頁複製連線資訊，填入 `.env`：

```bash
cp .env.example .env
# 編輯 .env，填入：
#   CLICKHOUSE_HOST=xxx.clickhouse.cloud
#   CLICKHOUSE_PORT=8443
#   CLICKHOUSE_USER=default
#   CLICKHOUSE_PASSWORD=<your-password>
#   CLICKHOUSE_SECURE=true
#   CLICKHOUSE_DATABASE=default
```

## 3. 驗證 MCP 連線

```bash
chmod +x scripts/test_mcp_clickhouse.sh
./scripts/test_mcp_clickhouse.sh
```

**DoD**：輸出 `✓ MCP server connected and ran SELECT 1 successfully.`

## 4. 驗證 ADK ↔ MCP ↔ Gemini 閉環

### 4.1 取得 Google 認證

兩條路擇一，`.env` 填其中一組：

| 路徑 | 設定 | 何時用 |
|---|---|---|
| **(a) AI Studio API key**（建議先用） | `GOOGLE_API_KEY=...`，`GOOGLE_GENAI_USE_VERTEXAI` 留空 | 免費、即時，不需 billing。M0 舉證足夠 |
| (b) Vertex AI | `GOOGLE_GENAI_USE_VERTEXAI=true` ＋ `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | GCP credits 核發後、要用 hackathon 帳號計費時 |

API key 申請：https://aistudio.google.com/apikey

模型預設 `gemini-2.5-flash`，可用 `M0_MODEL` 覆寫。

### 4.2 執行

```bash
./scripts/run_m0_roundtrip.sh
```

腳本自行寫出 `docs/m0-mcp-trace.log`（連線 host 與密碼會自動遮蔽）。

**DoD**：log 中出現 `FunctionCall`（tool `run_query` ＋ Gemini 自選的 SQL）、
對應的 `FunctionResponse`，結尾為 `result: PASS`。

### 4.3 兩個已知的環境坑（wrapper 已處理，別自己手打 uv 指令）

1. **不要把 `mcp-clickhouse` 裝進 ADK 那個環境。** 它 pin 的 `mcp` 版本比
   google-adk 需要的舊，同環境安裝會炸
   `ImportError: cannot import name 'SamplingCapability' from 'mcp'`。
   MCP server 是由腳本另外開 `uv run --with mcp-clickhouse` 子行程跑的，
   client 端只需要 `--with google-adk --with 'mcp<2'`。
2. **conda 會劫持 uv 的 ephemeral env。** 本機有 active 的 miniconda
   ＋ `PYTHONPATH`，`uv run` 會解析到 miniconda 的 site-packages，
   讓舊的 `mcp` 蓋掉 uv 剛裝的。wrapper 用
   `env -u PYTHONPATH -u PYTHONHOME -u CONDA_PREFIX -u CONDA_DEFAULT_ENV`
   擋掉。

已驗證可用的組合：`google-adk 2.7.1` ＋ `mcp 1.29.0` ＋ `mcp-clickhouse 0.4.1`
（`mcp 2.x` 重構了模組路徑，google-adk 2.7.1 還不相容，所以要 pin `mcp<2`）。

MCPToolset 掛載成功時會 discover 到三個 tool：
`list_databases`、`list_tables`、`run_query`。

## 5. GitHub About 區授權標籤

Repo 已設為 public：`https://github.com/erickh826/greenlight-agent`

首次 push 後，到 GitHub repo 頁面：

1. 右側 **About** → ⚙️ 齒輪
2. **License** 選 `Apache License 2.0`
3. 儲存

GitHub 通常會自動從根目錄 `LICENSE` 偵測；若未顯示，手動設定即可。

## 檢查清單

- [x] GCP credits 表單已送出，coupon 已 redeem（HKD$784.33 ≈ USD$100）
- [x] ClickHouse Cloud 服務已建立
- [x] `.env` 已填入連線資訊（**不要 commit**）
- [x] `./scripts/test_mcp_clickhouse.sh` 通過
- [x] MCPToolset 掛載 `mcp-clickhouse`，discover 到 3 個 tool
- [x] Vertex AI 路徑打通（billing 綁定 ＋ `aiplatform.googleapis.com` ENABLED ＋ ADC quota project）
- [x] `./scripts/run_m0_roundtrip.sh` 通過，`docs/m0-mcp-trace.log` 已產出
- [ ] GitHub About 區顯示 Apache-2.0
