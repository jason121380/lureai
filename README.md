# lure ai 輔導大腦

ChatGPT 式的設計師 1 對 1 AI 輔導系統（私有 RAG）。

## 功能

- ChatGPT 式聊天介面（串流回覆、AI 對話命名、關聯問題選項）、localStorage 對話紀錄與來源抽屜
- SQLite FTS5 + 中文 n-gram RAG
- 公開部署索引：209 塊人工重點整理知識（輔導 138 ＋ 店務營運 71）＋ 1.2 萬筆問法索引
- 267 份來源逐檔 Markdown、270 份去識別化對話案例
- `0.72` 最低信心門檻，低信心內容不進入答案
- 建議問題保證答得出來：連續追問 50 輪以上不會出現「需要人工協助」（`tests/test_followup_chain.py` 實跑驗證）
- 開場建議問題每次隨機（24 題題庫逐題驗證答得出來，`/api/health` 隨機出 12 題、前端抽 3 題）
- 回答一律「一句結論＋動詞開頭的行動條列」，每點附 `[n]` 引用；缺引用會自動重試一次，全形引用自動正規化
- 語氣設定可切換：專家模式（條列講深講透、附驗收數字）／客服模式（像真人聊天一句一句回，逐句顯示成訊息泡泡）
- 模型降級可觀測：前端顯示降級原因標籤，伺服器 log 有 `[boot]` 開機摘要與 `[llm]` 失敗原因（不含金鑰與問題內容）
- 個資、醫療、法律賠償與勞資話題轉人工
- 管理端知識編輯台：兩大主題總覽、知識庫新增/編輯、品質檢查、帳號與系統健康
- OpenAI Responses API 與 GPT-5.6 Luna
- 使用者帳密登入、HttpOnly session、後台帳號建立與密碼重設
- 統一帳號登入；帳號權限分「一般用戶」與「管理者」，管理者登入後由側欄「設定」icon 進入 `/admin` 管理後台（非管理者會被導回對話頁）
- 每位使用者本月 token、台幣花費與預算進度；超出 `MONTHLY_BUDGET_TWD` 時自動停用模型生成、改用抽取式回答
- 聊天每分鐘限流（`CHAT_RATE_LIMIT_PER_MINUTE`）、登入失敗限流、安全標頭與同源檢查
- 未設定 API Key 時可使用離線抽取式回答

## Zeabur 部署

在服務的環境變數使用 Raw 編輯模式貼上：

```dotenv
APP_PROFILE=designer_coach
ADMIN_TOKEN=請換成長且不可猜測的隨機值
USER_USERNAME=designer
USER_PASSWORD=請換成至少4字的密碼
USER_ROLE=admin
LLM_BASE_URL=https://api.openai.com
LLM_API_KEY=你的OpenAI_API_Key
LLM_MODEL=gpt-5.6-luna
LLM_REASONING_EFFORT=low
LLM_TIMEOUT_SECONDS=60
LLM_INPUT_USD_PER_MILLION=0.20
LLM_CACHED_INPUT_USD_PER_MILLION=0.02
LLM_CACHE_WRITE_USD_PER_MILLION=0.25
LLM_OUTPUT_USD_PER_MILLION=1.20
USD_TO_TWD=32.5
MONTHLY_BUDGET_TWD=1000
```

Zeabur 會注入 `PORT`，程式會自動讀取，不必設定 `APP_PORT`。

倉庫內含 `Dockerfile`（基底映像走 AWS ECR Public 鏡像，避開 Docker Hub 匿名下載限流造成的 429 建置失敗），Zeabur 會自動採用，`ZBPACK_*` 變數不再需要。Dockerfile 已內建部署韌性：預設綁 `0.0.0.0`（不必設 `APP_HOST`）、開機索引重建失敗只記錄不中斷、未設 `ADMIN_TOKEN` 時自動改用隨機權杖（等於停用 header 管理 API，後台仍以管理者帳號登入）。開機 log 會印 `[boot] profile=… chunks=… db=… model=…`，模型呼叫失敗會印 `[llm]` 原因，部署卡住時先看這兩行。

設計師輔導部署預設包含 209 塊已核准、去識別化的 RAG 區塊。原始檔、原始 Markdown、人員聯絡名冊、員工個資表單與未遮罩對話不會進入 GitHub。若要改用不公開的自訂索引，可透過私人 Git 倉庫、私有物件儲存或持久化 Volume 放入 JSONL，再設定：

```dotenv
KNOWLEDGE_JSONL=/data/hair-brain/designer_coach_full.jsonl
APP_DB_PATH=/data/hair-brain/designer_coach.db
```

SQLite 是工作資料庫（索引、檢索、當下狀態）。**持久化走 PostgreSQL、不需要 Volume**：在 Zeabur 加一個 PostgreSQL 服務並綁定到本服務（`DATABASE_URL`／`POSTGRES_*` 變數會自動注入），帳號、session、用量稽核、回饋評分與後台自訂知識會定期（預設 120 秒、可用 `PG_BACKUP_INTERVAL_SECONDS` 調整）壓成快照存進 Postgres，重新部署時自動還原；知識索引照常由 JSONL 重建，不進快照。沒綁 Postgres 時這些資料只活在容器內，重新部署會歸零。設定 `USER_USERNAME` 與 `USER_PASSWORD` 可在空資料庫自動建立第一個前台帳號，之後也能在管理後台建立或重設帳號。

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

- 輔導介面：<http://127.0.0.1:8765>
- 管理後台：<http://127.0.0.1:8765/admin>（需以「管理者」權限帳號登入；非管理者會被導回對話頁）

`ADMIN_TOKEN` 僅用於 API（`X-Admin-Token` header，供 curl、測試與緊急操作）；本機綁 127.0.0.1 時預設 `local-admin`，正式部署建議設成長且不可猜測的值。正式環境未設定時不會啟動失敗，而是自動產生隨機權杖並在 stderr 警告——此時 header 管理 API 等同停用，管理後台仍可用管理者帳號登入。

## 串接 OpenAI GPT-5.6 Luna

API Key 只放在後端環境變數，不要放進 JavaScript、Git 或瀏覽器儲存空間。

```bash
export LLM_BASE_URL="https://api.openai.com"
export LLM_API_KEY="你的 OpenAI API Key"
export LLM_MODEL="gpt-5.6-luna"
export LLM_REASONING_EFFORT="low"
export ADMIN_TOKEN="請設定長且不可猜測的管理權杖"
export USER_USERNAME="designer"
export USER_PASSWORD="請設定至少4字的密碼"
python3 run.py --port 8765
```

檢查是否啟用模型：

```bash
curl http://127.0.0.1:8765/api/health
```

應回傳 `"model_enabled": true`。系統透過 OpenAI Responses API 生成答案；模型呼叫失敗或輸出缺少引用時，API 會回傳 `model_status` 並清楚標示已降級為來源抽取式回答。

管理後台的系統健康分頁提供完整檢查；也可以用管理權杖直接打 API：

```bash
curl -H "X-Admin-Token: $ADMIN_TOKEN" \
  http://127.0.0.1:8765/api/admin/health
```

回傳 Server、內部 API、Frontend、SQLite Database、Postgres 持久化、Auth、RAG、Knowledge 與 LLM 九項狀態、延遲及安全化細節。`warning` 表示服務仍可運作但有降級，例如未設定 LLM 時使用抽取式回答；`error` 表示該元件需要處理。LLM 檢查會驗證 API Key 與模型存取權，但不會發送付費生成請求，也不會回傳 API Key 或完整本機路徑。

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

知識庫分成兩個大主題（每塊知識的 `domain` 欄位）：

| domain | 名稱 | 來源手冊 | 區塊 |
| --- | --- | --- | --- |
| `coaching` | 設計師一對一行銷輔導 | `designer_coaching_process.md` | coach-01~44 |
| `coaching` | 私訊對話健檢與成交 | `messaging_audit_playbook.md` | chat-01~30 |
| `coaching` | 廣告投放 | `ads_playbook.md` | ads-01~23 |
| `coaching` | 社群與版面輔導 | `social_playbook.md` | social-01~25 |
| `coaching` | 一對一輔導流程 | `session_playbook.md` | session-01~16 |
| `operations` | 店務營運管理 | `salon_operations_playbook.md` | ops-01~71 |

每塊知識另外帶一組「問法索引」（`aliases`）：設計師實際會怎麼問這塊知識，
種子寫在 `config/question_bank.json`，編譯時展開成一萬多筆。問法只進檢索欄位，
不會出現在回答或引用內容裡。

六本手冊都是人工重點整理：原始教材是掃描 OCR 與試算表傾印（`A1=` 儲存格、空白表單、公式），
無法直接引用，已抽成完整句子的方法與流程，原始語料封存於 `knowledge/archive/legacy_source_documents.jsonl.gz`。
編譯方式：

```bash
python3 scripts/build_knowledge_index.py knowledge/designer_coaching_process.jsonl --reviewed-at YYYY-MM-DD
python3 scripts/coverage_report.py    # 問法覆蓋率報告
```

沒有 `domain` 的舊資料由 `app/domains.py` 依分類與來源檔推斷，重建索引後自動歸位。

程式依以下順序選擇知識檔：

1. `KNOWLEDGE_JSONL` 指定的檔案
2. 本機存在的 `private_sources/full/rag/{profile}_full.jsonl`
3. 公開倉庫的去識別化部署 JSONL

部署索引：

```text
knowledge/designer_coaching_process.jsonl
```

其人工審核來源為 `knowledge/designer_coaching_process.md`。完整資料產物位於 Git 忽略的 `private_sources/full/`：

```text
private_sources/full/
├── extracted/       # 267 份來源各一份 MD，保留頁碼、投影片、工作表或段落定位
├── conversations/   # 270 份去識別化 1 對 1 輔導案例 MD
├── rag/             # 完整內部輔導 JSONL
└── manifest.json    # 每檔雜湊、狀態、摘要、警告與統計
```

對話原始匯出共有 270 個對話、12,664 則訊息，經去識別化及有重疊的長度切分後形成 491 個案例 chunks，再與簡報、試算表、文件、PDF、圖片 OCR 及核准流程知識合併。歷史教材中的價格、時程、制度、活動與效果只作案例，不能當現行資訊。

完整處理結果：243 份成功抽取、5 份暫存檔略過、3 份系統檔只記錄中繼資料、15 份密碼保護檔無法讀取內文、1 份損壞舊簡報無法復原。所有 267 份仍各自具有狀態 MD；系統不會把無法讀取的檔案標成已抽取。

匯入器只接受同時符合以下條件的資料：

- `rag_allowed=true`（舊格式 `customer_service_allowed=true` 僅供相容）
- `review_status=approved`
- `access_level` 必須與目前 profile 完全相符
- `chunk_id`、`title`、`source_file`、`locator`、`text` 完整

指定自己的核准 JSONL：

```bash
export KNOWLEDGE_JSONL="/absolute/path/to/approved-knowledge.jsonl"
python3 run.py --reindex-only
```

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
python3 run.py --reindex-only
python3 scripts/verify_full_knowledge.py
```

驗證報告在 `qa/full_knowledge_verification.json`。影片目前抽取檔案中繼資料與取樣畫面的 OCR，不包含語音轉錄。

### Profile

| Profile | Access level | 知識檔 | DB |
| --- | --- | --- | --- |
| `designer_coach`（唯一） | `internal_coaching` | `designer_coaching_process.jsonl` | `data/designer_coach.db` |

聊天與用量 API 需要先透過 `POST /api/auth/login` 建立 session。登入後呼叫 `POST /api/chat`，JSON body 為 `{"message":"問題","conversation_id":"可選 ID"}`；`GET /api/usage` 只回傳目前登入使用者的本月用量。前端可先讀取公開的 `GET /api/health` 確認目前 profile、知識筆數與模型是否啟用。

## 設定

- 檢索門檻與 top-k：`config/settings.json`
- 輔導回答規則：`config/designer_coach_policy.md`
- 敏感問題分類：`app/policy.py`
- 環境變數範例：`.env.example`

## 測試

```bash
python3 -m unittest discover -s tests -v
```

瀏覽器端到端測試位於 `tests/browser_smoke.py`，需要 Playwright 測試環境與本機 Chrome。
