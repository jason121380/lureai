# Hair Brain

ChatGPT 式美髮 AI 客服與嚴格 RAG 系統。專案包含中文檢索、來源引用、低信心拒答、敏感問題轉人工、管理後台與查詢稽核。

## 功能

- 客服聊天介面、localStorage 對話紀錄與來源抽屜
- SQLite FTS5 + 中文 n-gram RAG
- 15 個已核准、附來源定位的客服知識 chunks
- `0.72` 最低信心門檻，低信心內容不進入答案
- 價格、療效、退款、賠償、個資與即時預約轉人工
- 管理端知識搜尋、檢索測試、重新索引與 audit log
- OpenAI Chat Completions 相容接口
- 未設定 API Key 時可使用離線抽取式回答

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

匯入器只接受同時符合以下條件的資料：

- `customer_service_allowed=true`
- `review_status=approved`
- `access_level=customer_service`
- `chunk_id`、`title`、`source_file`、`locator`、`text` 完整

指定自己的核准 JSONL：

```bash
export KNOWLEDGE_JSONL="/absolute/path/to/approved-knowledge.jsonl"
python3 run.py --reindex-only
```

完整內部索引不會被客服程式讀取。

## 設定

- 檢索門檻與 top-k：`config/settings.json`
- 模型回答規則：`config/customer_policy.md`
- 敏感問題分類：`app/policy.py`
- 環境變數範例：`.env.example`

## 測試

```bash
python3 -m unittest discover -s tests -v
```

瀏覽器端到端測試位於 `tests/browser_smoke.py`，需要 Playwright 測試環境與本機 Chrome。
