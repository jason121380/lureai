# CLAUDE.md

Claude Code 工作指南。修改本專案前先讀這份文件與 `MEMORY.md`（決策紀錄）、`STYLE.md`（視覺規範）、`brain.md`（回覆規則總覽：語氣、長短、引用、追問；改回覆行為要同步它）。

## 專案概觀

lure ai（原 hair_brain）：美髮沙龍的私有 RAG 助理，單一 profile `designer_coach`（設計師 1 對 1 輔導；客服版已於 2026-08-31 移除）。零第三方依賴：Python 標準庫 HTTP server + SQLite FTS5 + vanilla JS 前端，模型走 OpenAI Responses API（未設定時降級抽取式回答）。

## 常用指令

```bash
python3 -m unittest discover -s tests        # 全部測試（必須全綠才能 push）
python3 scripts/build_knowledge_index.py knowledge/designer_coaching_process.jsonl --reviewed-at YYYY-MM-DD  # 改完 knowledge/*.md 必跑
python3 scripts/coverage_report.py           # 檢索覆蓋率報告
python3 run.py --reindex-only                # 重建索引
python3 run.py --port 8765                   # 啟動（designer_coach）
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
| `app/policy.py` | 敏感話題攔截（只擋人才能決定的事）、0.72 信心門檻 |
| `app/followups.py` | 建議問題規劃：每個追問都先驗證答得出來，不足時從相鄰知識補 |
| `app/storage.py` | SQLite schema 與所有查詢（一律在 `_lock` 內）|
| `app/health.py` | 管理端 8 項健康檢查 |
| `app/replica.py` | Postgres 快照持久化（不掛 Volume）：帳號／session／稽核／評分／自訂知識定期備份、開機還原 |
| `app/domains.py` | 兩大主題（店務營運管理／設計師一對一行銷輔導）的定義與歸類規則 |
| `app/curation.py` | 知識品質檢查（零碎、遮罩過多、標題無意義）|
| `config/synonyms.json` | 檢索同義詞層，可直接擴充讓 AI 聽懂更多說法（避免加入「流程／方法」這類泛用詞）|
| `knowledge/*.md` | 六本人工整理的知識手冊（coach／chat／ads／social／session／ops），編譯後共 209 塊 |
| `config/question_bank.json` | 問法索引種子：設計師實際會怎麼問，編譯時展開成 1.2 萬筆檢索別名 |
| `scripts/build_knowledge_index.py` | 唯一的索引編譯器（手冊 → JSONL，含問法展開）|
| `scripts/coverage_report.py` | 用問法索引量測檢索覆蓋率 |
| `static/` | chat（index.html+chat.js）與 admin（admin.html+admin.js），共用 `app.css` |

## 不可破壞的約定

- **認證模型**：統一帳號登入。`/admin` 頁面只認 admin 角色 session（非 admin 直接導回 `/`）；`X-Admin-Token` header 仍可打管理 API（curl／測試／緊急用），UI 沒有權杖輸入。
- **改知識就要重編索引**：`knowledge/*.md` 是唯一的知識來源，改完一定要跑 `scripts/build_knowledge_index.py`，否則測試會擋（`test_written_index_matches_the_playbooks`）。
- **問法索引不是答案**：`aliases` 只進檢索欄位，不會被引用或輸出。
- **追問不能斷**：建議問題一律經 `FollowupPlanner` 驗證（政策不擋＋撈得到夠格知識），`tests/test_followup_chain.py` 會實跑 50 輪連續追問，任何一輪轉人工就算失敗。
- **語氣設定**：chat API 的 `tone`（`expert` 預設／`service`）只切換輸出格式指令（`app/answer.py` `TONE_INSTRUCTIONS`）與前端渲染（客服模式逐行泡泡）；未知值一律當 expert，引用守門與追問規劃不受影響。
- **引用守門**：模型回答每點都要附 `[n]` 引用；全形引用（【1】（1）〔１〕）會被正規化成 `[1]`，仍缺引用就自動帶警語重試一次（串流與非串流路徑都有，用量兩次都記帳）。改 `app/answer.py` 時勿拆掉 `normalize_citation_marks` 與 `retry_with_citations`。
- **開場題庫**：`run.py` 的歡迎題庫每題都必須答得出來，`tests/test_welcome_prompts.py` 逐題驗證；`/api/health` 回傳隨機 12 題、前端每次抽 3 題。
- **ADMIN_TOKEN 可以不設**：未設定時自動改用隨機權杖（stderr 有警語），等於停用 header 管理 API，後台仍走 admin 帳號登入——這是部署韌性設計，不要改回缺少就 exit。
- **知識即重點整理**：索引只收人工整理過的手冊內容，不放 OCR 原文或表單傾印。要新增知識就改 `knowledge/*.md` 再用 `scripts/build_knowledge_index.py` 編譯，原始 OCR 語料封存在 `knowledge/archive/legacy_source_documents.jsonl.gz`。
- **兩大主題**：每塊知識都屬於 `domain`＝`operations`（店務營運管理）或 `coaching`（設計師一對一行銷輔導）。資料列自帶 `domain` 優先，沒帶時由 `app/domains.py` 依分類與來源推斷；後台總覽、篩選與新增知識都以這兩個主題為軸。
- **知識治理**：匯入強制 `review_status=approved` + `access_level` 相符 + `rag_allowed=true`；任何一筆不合格整批拒絕。
- **安全**：全部 SQL 參數化；回應含安全標頭與 HTML CSP（無 inline script）；POST 檢查 Origin；月預算超標即停用模型生成；聊天與登入均有限流。
- **零依賴（一個例外）**：不要引入第三方 Python 套件（Playwright 只用於本機測試）。唯一例外是 `psycopg`：只在 Dockerfile 安裝、只在設定了 Postgres 連線時 import（`app/replica.py` 守門），本機開發不需要它。
- **持久化走 Postgres 快照，不掛 Volume**（使用者決定）：SQLite 是可拋棄的工作庫；`app/replica.py` 把 users／sessions／audits／feedback／自訂知識壓成 gzip JSON 單列快照存 Postgres，開機還原、定期備份（內容沒變不上傳）。改 durable 資料表 schema 時記得欄位取交集的還原邏輯已涵蓋新增欄位，但刪欄位要同步看 `apply_snapshot`。
- **health check 標記**：`app/health.py` `_frontend_check` 會驗證前端檔案內含特定字串（如 `.chat-main`、`id="admin-shell"`、`/api/chat`）；改前端時勿移除。
- **後台定位**：知識編輯台（總覽／知識庫／品質檢查／帳號／系統健康）。可直接新增編輯知識，存 SQLite（`chunks.origin='custom'`），重建索引不會被覆蓋；`匯出 JSONL` 可下載回存 repo 永久化。
- 對話紀錄存 localStorage（per user id），伺服器不存聊天內容，只存稽核（問題已遮罩 PII）。

## 工作流程

- 分支：`claude/codex-review-optimization-sw0j31`（或當前指定分支）→ push → **開 PR 併入 main（使用者已授權每次 push 後自動 merge）**。main 一更新 Zeabur 就自動部署。
- push 前：`python3 -m unittest discover -s tests` 必須全綠；UI 改動用 Playwright 截圖驗證（chromium 在 `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`）。
- commit 訊息、PR、程式註解不得出現模型名稱。
- 正式環境除錯：Zeabur Log 搜 `[boot]`（開機摘要：profile／chunks／db／model）與 `[llm]`（模型失敗階段與原因，不含金鑰與問題內容）。
