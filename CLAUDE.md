# CLAUDE.md

Claude Code 工作指南。修改本專案前先讀這份文件與 `MEMORY.md`（決策紀錄）、`STYLE.md`（視覺規範）。

## 專案概觀

lure ai（原 hair_brain）：美髮沙龍的私有 RAG 助理，兩個 profile——`customer_service`（客服）與 `designer_coach`（設計師 1 對 1 輔導）。零第三方依賴：Python 標準庫 HTTP server + SQLite FTS5 + vanilla JS 前端，模型走 OpenAI Responses API（未設定時降級抽取式回答）。

## 常用指令

```bash
python3 -m unittest discover -s tests        # 全部測試（必須全綠才能 push）
python3 run.py --reindex-only                # 重建索引
python3 run.py --port 8765                   # 啟動（客服 profile）
python3 run.py --profile designer_coach --port 8766
```

本機開發免設定：`ADMIN_TOKEN` 預設 `local-admin`（僅綁 127.0.0.1 時）；用 `USER_USERNAME`/`USER_PASSWORD`/`USER_ROLE=admin` 建第一個帳號。

## 架構地圖

| 檔案 | 職責 |
| --- | --- |
| `run.py` | CLI 入口、profile 定義、路徑解析 |
| `app/server.py` | HTTP 路由、認證整合、安全標頭、限流、預算檢查、靜態檔案（ETag/快取、乾淨路由 `/`、`/admin`） |
| `app/auth.py` | scrypt 密碼、session、角色（user/admin）、登入與聊天限流器 |
| `app/service.py` | 聊天編排：預檢 → 檢索 → 政策 → 回答 → 稽核（含 PII 遮罩） |
| `app/retrieval.py` | FTS5 + CJK bigram 重排序 |
| `app/answer.py` | OpenAI Responses API 呼叫與抽取式降級；timeout 由 `LLM_TIMEOUT_SECONDS` 控制 |
| `app/policy.py` | 敏感話題攔截、0.72 信心門檻 |
| `app/storage.py` | SQLite schema 與所有查詢（一律在 `_lock` 內）|
| `app/health.py` | 管理端 8 項健康檢查 |
| `static/` | chat（index.html+chat.js）與 admin（admin.html+admin.js），共用 `app.css` |

## 不可破壞的約定

- **認證模型**：統一帳號登入。`/admin` 頁面只認 admin 角色 session（非 admin 直接導回 `/`）；`X-Admin-Token` header 仍可打管理 API（curl／測試／緊急用），UI 沒有權杖輸入。
- **知識治理**：匯入強制 `review_status=approved` + `access_level` 相符 + `rag_allowed=true`；任何一筆不合格整批拒絕。兩個 profile 的 DB 與知識檔完全隔離。
- **安全**：全部 SQL 參數化；回應含安全標頭與 HTML CSP（無 inline script）；POST 檢查 Origin；月預算超標即停用模型生成；聊天與登入均有限流。
- **零依賴**：不要引入第三方 Python 套件（Playwright 只用於本機測試）。
- **health check 標記**：`app/health.py` `_frontend_check` 會驗證前端檔案內含特定字串（如 `.chat-main`、`id="admin-shell"`、`/api/chat`）；改前端時勿移除。
- 對話紀錄存 localStorage（per user id），伺服器不存聊天內容，只存稽核（問題已遮罩 PII）。

## 工作流程

- 分支：`claude/codex-review-optimization-sw0j31`（或當前指定分支）→ push → **開 PR 併入 main（使用者已授權每次 push 後自動 merge）**。main 一更新 Zeabur 就自動部署。
- push 前：`python3 -m unittest discover -s tests` 必須全綠；UI 改動用 Playwright 截圖驗證（chromium 在 `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`）。
- commit 訊息、PR、程式註解不得出現模型名稱。
