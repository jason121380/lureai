# Hair Brain

ChatGPT 式美髮 AI 客服與設計師 1 對 1 AI 輔導系統。兩種 RAG profile 使用不同知識檔、資料庫、回答規則與瀏覽器對話空間。

## 功能

- 客服聊天介面、localStorage 對話紀錄與來源抽屜
- SQLite FTS5 + 中文 n-gram RAG
- 15 個已核准客服知識 chunks
- 25 個去識別化、附來源雜湊的設計師輔導 chunks
- `customer_service`／`designer_coach` 雙 profile 隔離
- `0.72` 最低信心門檻，低信心內容不進入答案
- 價格、療效、退款、賠償、個資與即時預約轉人工
- 管理端知識搜尋、檢索測試、重新索引與 audit log
- OpenAI Chat Completions 相容接口
- 未設定 API Key 時可使用離線抽取式回答

## 啟動設計師 AI 輔導

```bash
python3 run.py --profile designer_coach --reindex-only
python3 run.py --profile designer_coach --port 8766
```

- 輔導介面：<http://127.0.0.1:8766>
- 輔導管理後台：<http://127.0.0.1:8766/admin.html>

macOS 也可以執行 `./start-coach.command`。客服與輔導可分別使用 `8765`、`8766` 同時運作。

## Zeabur 部署

在服務的環境變數使用 Raw 編輯模式貼上：

```dotenv
ZBPACK_PYTHON_ENTRY=run.py
ZBPACK_PYTHON_VERSION=3.12
ZBPACK_START_COMMAND="python3 run.py --reindex-only && _startup"
APP_HOST=0.0.0.0
APP_PROFILE=designer_coach
ADMIN_TOKEN=請換成長且不可猜測的隨機值
LLM_BASE_URL=https://api.openai.com
LLM_API_KEY=你的OpenAI_API_Key
LLM_MODEL=gpt-5.6-luna
```

Zeabur 會注入 `PORT`，程式會自動讀取，不必設定 `APP_PORT`。若部署客服版本，將 `APP_PROFILE` 改成 `customer_service`。不要設定 `KNOWLEDGE_JSONL` 或 `APP_DB_PATH`，讓 profile 自動選擇隨附知識與獨立資料庫。

## 系統需求

- Python 3.11 或更新版本
- SQLite 需支援 FTS5（一般 Python 安裝預設支援）
- 不需要安裝第三方 Python 套件

## 啟動

```bash
git clone https://github.com/jason121380/hair_brain.git
cd hair_brain
python3 run.py --reindex-only
python3 run.py --port 8765
```

- 客服介面：<http://127.0.0.1:8765>
- 管理後台：<http://127.0.0.1:8765/admin.html>
- 本機預設管理權杖：`local-admin`

正式部署必須以 `ADMIN_TOKEN` 更換預設管理權杖。

## 串接 OpenAI GPT-5.6 Luna

API Key 只放在後端環境變數，不要放進 JavaScript、Git 或瀏覽器儲存空間。

```bash
export LLM_BASE_URL="https://api.openai.com"
export LLM_API_KEY="你的 OpenAI API Key"
export LLM_MODEL="gpt-5.6-luna"
export ADMIN_TOKEN="請設定長且不可猜測的管理權杖"
python3 run.py --port 8765
```

檢查是否啟用模型：

```bash
curl http://127.0.0.1:8765/api/health
```

應回傳 `"model_enabled": true`。模型呼叫失敗或輸出缺少引用時，系統會降級為來源抽取式回答。

### macOS Keychain

```bash
read -s "OPENAI_KEY?OpenAI API Key: "
echo
security add-generic-password -U -a "$USER" -s "hair-brain-openai" -w "$OPENAI_KEY"
unset OPENAI_KEY

export LLM_API_KEY="$(security find-generic-password -a "$USER" -s "hair-brain-openai" -w)"
export LLM_BASE_URL="https://api.openai.com"
export LLM_MODEL="gpt-5.6-luna"
python3 run.py
```

## 知識資料

預設載入：

```text
knowledge/active_customer_service.jsonl
```

設計師教練載入：

```text
knowledge/designer_coaching_process.jsonl
```

其人工審核來源為 `knowledge/designer_coaching_process.md`。原始對話匯出含個資、帳務、法律個案、歷史價格及逐筆業績，只保留在 Git 忽略的 `private_sources/`，不會進入正式索引或 GitHub。

匯入器只接受同時符合以下條件的資料：

- 客服舊格式使用 `customer_service_allowed=true`，新格式使用 `rag_allowed=true`
- `review_status=approved`
- `access_level` 必須與目前 profile 完全相符
- `chunk_id`、`title`、`source_file`、`locator`、`text` 完整

指定自己的核准 JSONL：

```bash
export KNOWLEDGE_JSONL="/absolute/path/to/approved-knowledge.jsonl"
python3 run.py --reindex-only
```

完整內部索引不會被客服程式讀取。

### Profile 對照

| Profile | Access level | 知識檔 | DB |
| --- | --- | --- | --- |
| `customer_service` | `customer_service` | `active_customer_service.jsonl` | `data/knowledge.db` |
| `designer_coach` | `internal_coaching` | `designer_coaching_process.jsonl` | `data/designer_coach.db` |

自己的介面呼叫方式相同：`POST /api/chat`，JSON body 為 `{"message":"問題","conversation_id":"可選 ID"}`。前端可先讀取 `GET /api/health` 確認目前 profile、知識筆數與模型是否啟用。

## 設定

- 檢索門檻與 top-k：`config/settings.json`
- 模型回答規則：`config/customer_policy.md`
- 輔導回答規則：`config/designer_coach_policy.md`
- 敏感問題分類：`app/policy.py`
- 環境變數範例：`.env.example`

## 測試

```bash
python3 -m unittest discover -s tests -v
```

瀏覽器端到端測試位於 `tests/browser_smoke.py`，需要 Playwright 測試環境與本機 Chrome。
