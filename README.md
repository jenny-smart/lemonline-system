# line-lemonsystem — 專業 LINE 管理後台

Python + Turso + Streamlit 架構，補齊「訊息改用戶名稱」「下標籤」功能。

```
LINE → Cloudflare Worker (worker/, 既有,不動) → Turso line_messages (既有,不動)
                                                        │
                                              (新增表,不影響原表)
                                                        │
                                    line_users / tags / user_tags / message_status
                                                        │
                                              streamlit_app/ (全新管理後台)
```

## ⚠️ 部署前必看：欄位名稱要對過

我沒辦法直接讀你 Turso 裡 `line_messages` 的實際結構（GitHub 網頁擋掉了自動讀取），
所以 `db/client.py` 最上面的 `COL` 是**假設值**：

```python
COL = {
    "id": "id",
    "user_id": "line_user_id",
    "display_name": "display_name",
    "content": "message_text",
    "message_type": "message_type",
    "received_at": "received_at",
}
```

部署前請對照你 `worker/` 裡寫入 Turso 的 SQL，把這幾個值改成你實際的欄位名稱。
如果欄位名稱不一樣，整個查詢會直接報錯，一改就能跑起來。

## 資料庫變更

只有 `db/migration_001_add_users_and_tags.sql` 是新的，**不改動、不刪除** `line_messages`：

- `line_users` — 用戶主檔（改名、原始名稱、首次/最後互動、訊息數）
- `tags` — 標籤主檔
- `user_tags` — 用戶與標籤的多對多關聯
- `message_status` — 每則訊息的處理狀態（未回覆/已回覆/處理中...），用 message id 對應，不動原表

第一次啟動 app 時會自動執行這個 migration（`db.ensure_schema`），之後每次啟動都是安全的
no-op（`CREATE TABLE IF NOT EXISTS`）。

## Worker 端要補一行（讓新訊息自動建立/更新用戶）

在 `worker/` 寫入 `line_messages` 的地方（收到訊息後），額外呼叫一次
`db.upsert_user_seen(client, user_id, display_name)`（或是 Worker 是 JS 寫的話，
在 JS 那邊對 `line_users` 做等效的 upsert）。這樣 `line_users` 才會持續有新用戶進來、
互動次數才會累加。如果你想先不動 Worker，也可以：先跑一次性的回填腳本，
從既有 `line_messages` 生成 `line_users`（我可以另外幫你寫）。

## 安裝與本機執行

```bash
pip install -r requirements.txt
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# 編輯 secrets.toml,填入 TURSO_DATABASE_URL / TURSO_AUTH_TOKEN
streamlit run streamlit_app/Home.py
```

## 部署（Streamlit Community Cloud）

1. Push 這個 repo 到 GitHub
2. 到 share.streamlit.io 連接 repo，主檔選 `streamlit_app/Home.py`
3. App settings → Secrets，貼上 `TURSO_DATABASE_URL`、`TURSO_AUTH_TOKEN`

## 功能

- **儀表板**：總訊息數、今日訊息、未回覆數、用戶數、標籤數、近 7 天趨勢圖
- **訊息管理**：關鍵字/狀態/標籤篩選、逐則訊息改用戶名稱＋下標籤、切換處理狀態
- **用戶管理**：用戶清單、關鍵字/標籤篩選、改名稱＋下標籤
- **標籤管理**：新增/刪除標籤、設定顏色與描述

## 待辦

- [ ] 確認 `db/client.py` 的 `COL` 對應到你實際的 `line_messages` 欄位
- [x] Python 版本釘在 3.11(見 `runtime.txt`),避免 libsql-client 與新版 Python 不相容
- [ ] Worker 補上 `line_users` upsert，或跑一次性回填腳本
- [ ] `python_app/`（如果有既有邏輯）要不要併入或保留，看你原本用途
