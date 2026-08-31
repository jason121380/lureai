# Hair Brain

ChatGPT 式美髮 AI 客服與設計師 1 對 1 AI 輔導系統。兩種 RAG profile 使用不同知識檔、資料庫、回答規則與瀏覽器對話空間。

## 功能

- 客服聊天介面、localStorage 對話紀錄與來源抽屜
- SQLite FTS5 + 中文 n-gram RAG
- 本機完整索引：549 個客服 chunks、2,443 個設計師輔導 chunks
- 公開部署索引：15 個客服 chunks、2,393 個去識別化輔導 chunks
- 267 份來源逐檔 Markdown、270 份去識別化對話案例
- `customer_service`／`designer_coach` 雙 profile 隔離
- `0.72` 最低信心門檻，低信心內容不進入答案
- 價格、療效、退款、賠償、個資與即時預約轉人工
- 管理端知識搜尋、檢索測試、重新索引與 audit log
- OpenAI Responses API 與 GPT-5.6 Luna
- 使用者帳密登入、HttpOnly session、後台帳號建立與密碼重設
- 每位使用者本月 token、台幣花費與預算進度
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
USER_USERNAME=designer
USER_PASSWORD=請換成至少8字的強密碼
LLM_BASE_URL=https://api.openai.com
LLM_API_KEY=你的OpenAI_API_Key
LLM_MODEL=gpt-5.6-luna
LLM_REASONING_EFFORT=low
LLM_INPUT_USD_PER_MILLION=0.20
LLM_CACHED_INPUT_USD_PER_MILLION=0.02
LLM_CACHE_WRITE_USD_PER_MILLION=0.25
LLM_OUTPUT_USD_PER_MILLION=1.20
USD_TO_TWD=32.5
MONTHLY_BUDGET_TWD=1000
```

Zeabur 會注入 `PORT`，程式會自動讀取，不必設定 `APP_PORT`。若部署客服版本，將 `APP_PROFILE` 改成 `customer_service`。

設計師輔導部署預設包含 2,393 個已核准、去識別化的 RAG 區塊。原始檔、原始 Markdown、人員聯絡名冊、員工個資表單與未遮罩對話不會進入 GitHub。若要改用不公開的自訂索引，可透過私人 Git 倉庫、私有物件儲存或持久化 Volume 放入 JSONL，再設定：

```dotenv
KNOWLEDGE_JSONL=/data/hair-brain/designer_coach_full.jsonl
APP_DB_PATH=/data/hair-brain/designer_coach.db
```

客服部署則改用 `customer_service_full.jsonl` 與 `knowledge.db`。SQLite 負責索引、使用者、session、用量與稽核紀錄，不需要另外建立 PostgreSQL；正式環境建議掛載持久化 Volume，否則重新部署時後四項紀錄會重置。設定 `USER_USERNAME` 與 `USER_PASSWORD` 可在空資料庫自動建立第一個前台帳號，之後也能在管理後台建立或重設帳號。

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
export LLM_REASONING_EFFORT="low"
export ADMIN_TOKEN="請設定長且不可猜測的管理權杖"
export USER_USERNAME="designer"
export USER_PASSWORD="請設定至少8字的強密碼"
python3 run.py --port 8765
```

檢查是否啟用模型：

```bash
curl http://127.0.0.1:8765/api/health
```

應回傳 `"model_enabled": true`。系統透過 OpenAI Responses API 生成答案；模型呼叫失敗或輸出缺少引用時，API 會回傳 `model_status` 並清楚標示已降級為來源抽取式回答。

管理後台提供完整健康檢查，需使用管理權杖：

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
  http://127.0.0.1:8765/api/admin/health
```

回傳 Server、內部 API、Frontend、SQLite Database、Auth、RAG、Knowledge 與 LLM 八項狀態、延遲及安全化細節。`warning` 表示服務仍可運作但有降級，例如未設定 LLM 時使用抽取式回答；`error` 表示該元件需要處理。LLM 檢查會驗證 API Key 與模型存取權，但不會發送付費生成請求，也不會回傳 API Key 或完整本機路徑。

用量成本以 Responses API 回傳的 input、cached input、cache write 與 output tokens 計算。各 token 類型費率、台幣換算率與月預算都能由 `.env` 對應變數調整；模型費率或匯率變動時只需更新環境變數。

### macOS Keychain

```bash
read -s "OPENAI_KEY?OpenAI API Key: "
echo
security add-generic-password -U -a "$USER" -s "hair-brain-openai" -w "$OPENAI_KEY"
unset OPENAI_KEY

export LLM_API_KEY="$(security find-generic-password -a "$USER" -s "hair-brain-openai" -w)"
export LLM_BASE_URL="https://api.openai.com"
export LLM_MODEL="gpt-5.6-luna"
export LLM_REASONING_EFFORT="low"
python3 run.py
```

## 知識資料架構

程式依以下順序選擇知識檔：

1. `KNOWLEDGE_JSONL` 指定的檔案
2. 本機存在的 `private_sources/full/rag/{profile}_full.jsonl`
3. 公開倉庫的去識別化部署 JSONL

部署索引：

```text
knowledge/active_customer_service.jsonl
knowledge/designer_coaching_process.jsonl
```

設計師教練載入：

```text
knowledge/designer_coaching_process.jsonl
```

其人工審核來源為 `knowledge/designer_coaching_process.md`。完整資料產物位於 Git 忽略的 `private_sources/full/`：

```text
private_sources/full/
├── extracted/       # 267 份來源各一份 MD，保留頁碼、投影片、工作表或段落定位
├── conversations/   # 270 份去識別化 1 對 1 輔導案例 MD
├── rag/             # 客服與內部輔導兩份完整 JSONL
└── manifest.json    # 每檔雜湊、狀態、摘要、警告與統計
```

對話原始匯出共有 270 個對話、12,664 則訊息，經去識別化及有重疊的長度切分後形成 491 個案例 chunks，再與簡報、試算表、文件、PDF、圖片 OCR 及核准流程知識合併。歷史教材中的價格、時程、制度、活動與效果只作案例，不能當現行資訊。

完整處理結果：243 份成功抽取、5 份暫存檔略過、3 份系統檔只記錄中繼資料、15 份密碼保護檔無法讀取內文、1 份損壞舊簡報無法復原。所有 267 份仍各自具有狀態 MD；系統不會把無法讀取的檔案標成已抽取。

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

### 重建完整私人知識

完整抽取需要 Python 的 `python-pptx`、`openpyxl`、`python-docx`、`pypdf`、Pillow，以及 LibreOffice `soffice`。macOS 圖片與影片畫面 OCR 使用內附 Swift 程式：

```bash
mkdir -p private_sources/bin
swiftc scripts/vision_ocr.swift -o private_sources/bin/vision_ocr
swiftc scripts/video_frame_ocr.swift -o private_sources/bin/video_frame_ocr
python3 scripts/build_full_knowledge.py \
  /absolute/path/to/source-folder private_sources/full \
  --ocr-binary private_sources/bin/vision_ocr \
  --video-ocr-binary private_sources/bin/video_frame_ocr
python3 run.py --profile customer_service --reindex-only
python3 run.py --profile designer_coach --reindex-only
python3 scripts/verify_full_knowledge.py
```

驗證報告在 `qa/full_knowledge_verification.json`。影片目前抽取檔案中繼資料與取樣畫面的 OCR，不包含語音轉錄。

### Profile 對照

| Profile | Access level | 知識檔 | DB |
| --- | --- | --- | --- |
| `customer_service` | `customer_service` | `active_customer_service.jsonl` | `data/knowledge.db` |
| `designer_coach` | `internal_coaching` | `designer_coaching_process.jsonl` | `data/designer_coach.db` |

聊天與用量 API 需要先透過 `POST /api/auth/login` 建立 session。登入後呼叫 `POST /api/chat`，JSON body 為 `{"message":"問題","conversation_id":"可選 ID"}`；`GET /api/usage` 只回傳目前登入使用者的本月用量。前端可先讀取公開的 `GET /api/health` 確認目前 profile、知識筆數與模型是否啟用。

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
