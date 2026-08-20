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

需要 Google 認證：`.env` 中設 `GOOGLE_API_KEY`，或設
`GOOGLE_GENAI_USE_VERTEXAI=true` ＋ `GOOGLE_CLOUD_PROJECT`。
模型預設 `gemini-2.5-flash`，可用 `M0_MODEL` 覆寫。

```bash
uv run --python 3.13 --with google-adk --with mcp-clickhouse \
    scripts/m0_adk_roundtrip.py
```

腳本自行寫出 `docs/m0-mcp-trace.log`（連線 host 與密碼會自動遮蔽）。

**DoD**：log 中出現 `FunctionCall`（tool `run_query` ＋ Gemini 自選的 SQL）、
對應的 `FunctionResponse`，結尾為 `result: PASS`。

## 5. GitHub About 區授權標籤

Repo 已設為 public：`https://github.com/erickh826/greenlight-agent`

首次 push 後，到 GitHub repo 頁面：

1. 右側 **About** → ⚙️ 齒輪
2. **License** 選 `Apache License 2.0`
3. 儲存

GitHub 通常會自動從根目錄 `LICENSE` 偵測；若未顯示，手動設定即可。

## 檢查清單

- [ ] GCP credits 表單已送出
- [ ] ClickHouse Cloud 服務已建立
- [ ] `.env` 已填入連線資訊（**不要 commit**）
- [ ] `./scripts/test_mcp_clickhouse.sh` 通過
- [ ] `scripts/m0_adk_roundtrip.py` 通過，`docs/m0-mcp-trace.log` 已產出
- [ ] GitHub About 區顯示 Apache-2.0
