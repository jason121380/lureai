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
| `app/humanize.py` | LINE 出口的固定動作：去引用、去標點、拆則、回覆停頓，沒有可調參數 |
| `app/policy.py` | 敏感話題攔截（只擋人才能決定的事）、0.72 信心門檻 |
| `app/quality.py` | 回答品質守門：延後回答句型／承諾沒交付／不表態／不存在的角色，命中就重打一次 |
| `app/followups.py` | 建議問題規劃：每個追問都先驗證答得出來，不足時從相鄰知識補 |
| `app/storage.py` | SQLite schema 與所有查詢（一律在 `_lock` 內）|
| `app/health.py` | 管理端 9 項健康檢查（含 Postgres 持久化）|
| `app/replica.py` | Postgres 快照持久化（不掛 Volume）：帳號／session／稽核／評分／自訂知識定期備份、開機還原 |
| `app/domains.py` | 兩大主題（店務營運管理／設計師一對一行銷輔導）的定義與歸類規則 |
| `app/tuning.py` | AI 模型校調：把送給模型的規則整理成目錄，後台可逐條改；`compose_policy`／`compose_tone` 負責組回去 |
| `app/curation.py` | 知識品質檢查（零碎、遮罩過多、標題無意義）|
| `app/documents.py` | 上傳的檔案 → 純文字（docx／xlsx／pptx 是 ZIP，用標準庫 zipfile＋xml；PDF 用 zlib）|
| `app/extract.py` | 上傳的文件 → 候選知識（模型重寫，不通時走規則切法）|
| `config/synonyms.json` | 檢索同義詞層，可直接擴充讓 AI 聽懂更多說法（避免加入「流程／方法」這類泛用詞）|
| `knowledge/*.md` | 九本人工整理的知識手冊（coach／chat／ads／social／session／career／ops／script／metric），編譯後共 278 塊 |
| `config/question_bank.json` | 問法索引種子：設計師實際會怎麼問，編譯時展開成 1.4 萬筆檢索別名 |
| `scripts/build_knowledge_index.py` | 唯一的索引編譯器（手冊 → JSONL，含問法展開）|
| `scripts/coverage_report.py` | 用問法索引量測檢索覆蓋率 |
| `static/` | chat（index.html+chat.js）與 admin（admin.html+admin.js），共用 `app.css` |

## 不可破壞的約定

- **認證模型**：統一帳號登入。`/admin` 頁面只認 admin 角色 session（非 admin 直接導回 `/`）；`X-Admin-Token` header 仍可打管理 API（curl／測試／緊急用），UI 沒有權杖輸入。
- **改知識就要重編索引**：`knowledge/*.md` 是唯一的知識來源，改完一定要跑 `scripts/build_knowledge_index.py`，否則測試會擋（`test_written_index_matches_the_playbooks`）。
- **問法索引不是答案**：`aliases` 只進檢索欄位，不會被引用或輸出。
- **追問要不要帶脈絡，看分數高低不是看有沒有過門檻**：`app/service.py` 的 `WEAK_MATCH_SCORE = 0.80`。完整問題自己就撈得準（100 題開場題庫實測最低 0.867），而「我想寫得自然一點」「然後呢？」這種接話只有 0.748~0.773——它們是靠一個字勉強過 0.72，主題完全不對。低於 0.80 才補前兩題重撈，而且**只在補完分數更高時才換掉**，否則自足的問題會被前一題帶走。
- **追問不能斷**：建議問題一律經 `FollowupPlanner` 驗證（政策不擋＋撈得到夠格知識），`tests/test_followup_chain.py` 會實跑 50 輪連續追問，任何一輪轉人工就算失敗。
- **lurebot 大腦外接**：LINE 端（lurebot）自己沒有知識庫，一律打 `/api/bot/reply` 取回覆。這條路走 `X-Bot-Token`（env `BOT_API_TOKEN`，沒設定就整組關閉）與 `lurebot` 服務帳號記帳，檢索／政策／引用守門／稽核全部與 `/api/chat` 共用同一條 `service.chat`。三個硬規則：政策擋下（敏感題、低於 0.72）一律回 `escalated` 且 `messages` 為空，讓真人接手；模型降級的回覆回 `unavailable`，不進 LINE（`boundary` 邊界題與 `smalltalk` 閒聊／情緒的回答例外，它們本來就是寫給通訊軟體的短句——群組裡有人打招呼卻已讀不回最傷）；`[n]` 引用只在送出前的出口剝除（LINE 模式與客服模式一樣不做引用守門，否則少一個編號就整則不回）。**回覆行為沒有可調參數**：語氣、長短與斷句規則寫在 `line` 語氣裡，送出前的去標點／拆則／停頓寫死在 `app/humanize.py`（首則停頓 8-25 秒、每則之間至少 3 秒）。兩邊都不要再長出設定面板，要改就改 `line` 語氣或 `humanize.py`。
- **斷行要數字數，不是數行數**：客服／LINE 的「每行 12 字、一則 2 行、最多 3 則」在 `static/chat.js` 的 `wrapLine` 與 `app/humanize.py` 的 `wrap_line` 各有一份**同樣規則**的實作，改一邊要改兩邊。只數換行是擋不住的——模型常常回一整行 120 字、一個換行都沒有。三個不能拿掉的細節：字數只算內容不算空白、比較時兩邊都要去空白、先把 2 字以內的碎片黏回後面那段（否則會斷在「20」跟「則」中間）。
- **話術範本本來就該短**：`app/curation.py` 算散文比例前會先拿掉引用區塊（`> ...` 逐字稿）。那個檢查是為 OCR 碎片寫的，直接拿去量話術會把 script-* 判成「內容零碎」。
- **回答品質守門**：生成完之後 `app/quality.py` 用程式檢查七件事（整則只有「我陪你拆」、說要給成品卻沒給、要十個只給兩個、問到立場不表態、把人推給不存在的對象（「轉人工」「會有專人」——**「主管」不算**，沙龍當然有主管）、被質疑就無條件認錯（道歉收回立場又不給理由；有給理由或重新表態的不算，真的講錯本來就該改口）、同一則自相矛盾（「建議漲價」又「不要漲價」）），命中就帶著具體理由重打一次，**第二次還是不合格就送原本那則**——擋掉會變成降級訊息，比廢話更糟。判斷刻意保守，改規則前先讓 `tests/test_quality.py` 的反向案例（正常好回答）全綠。
- **客訴與退費不是敏感題**：`legal_refund_or_compensation` 只擋「提告／訴訟／法律責任／律師／求償／保證效果」。染壞要退費、客人要留負評這種每天都會遇到的現場題一律走檢索，擋掉等於這個產品最需要陪伴的時刻反而不說話。
- **語氣設定**：chat API 的 `tone`（`expert` 預設／`service`／`line`）只切換輸出格式指令（`app/answer.py` `TONE_INSTRUCTIONS`）與前端渲染（客服模式逐行泡泡）；未知值一律當 expert，追問規劃不受影響。
- **引用守門**：模型回答每點都要附 `[n]` 引用；全形引用（【1】（1）〔１〕）會被正規化成 `[1]`，仍缺引用就自動帶警語重試一次（串流與非串流路徑都有，用量兩次都記帳）。**守門只套用在專家模式**：客服模式的編號前端本來就會剝掉、LINE 模式在出口剝掉，硬要求會讓正常回覆被丟掉（客服看到降級訊息，LINE 直接不回話），因此由 `AnswerEngine.requires_citations(tone)` 放行這兩種。改 `app/answer.py` 時勿拆掉 `normalize_citation_marks`、`retry_with_citations` 與 `requires_citations`。
- **知識來源只列答案引用到的**：檢索一次撈三塊，但窄問題常常只有第一塊能用——模型每點都寫 `[1]` 是對的，錯的是把沒被引用的兩塊也掛成「知識來源 2、3」。`app/service.py` 的 `_fit_citations` 在回傳前砍掉沒被引用的來源，並把 `[n]` 重編成連號（引用 1、3 → 列兩則、答案裡的 `[3]` 變 `[2]`）。**只在會顯示編號的語氣做**（`requires_citations`）：客服／LINE 的編號在出口就被剝掉，照樣裁切會一則來源都不剩；一個都沒引用時也不裁，那是引用守門的問題。串流路徑改的是最終 result（前端以 result 為準覆蓋串流文字），所以重編號安全。
- **開場題庫**：`run.py` 的歡迎題庫共 100 題，每題都必須答得出來且不重複，`tests/test_welcome_prompts.py` 逐題驗證；`/api/health` 回傳整份打散的題庫、前端每次抽 5 題（`WELCOME_PROMPT_COUNT`）。加題目前先用檢索驗過分數有沒有過 0.72。
- **ADMIN_TOKEN 可以不設**：未設定時自動改用隨機權杖（stderr 有警語），等於停用 header 管理 API，後台仍走 admin 帳號登入——這是部署韌性設計，不要改回缺少就 exit。
- **知識即重點整理**：索引只收人工整理過的手冊內容，不放 OCR 原文或表單傾印。要新增知識就改 `knowledge/*.md` 再用 `scripts/build_knowledge_index.py` 編譯，原始 OCR 語料封存在 `knowledge/archive/legacy_source_documents.jsonl.gz`。
- **話術範本與關鍵數字是成品，不是原則**：`knowledge/scripts_playbook.md`（script-01~20）給的是可以直接複製貼上的逐字稿，`knowledge/benchmarks_playbook.md`（metric-01~11）給的是判斷高低的基準數字與拆解順序。這兩本是拿來解「說要給成品卻沒給」與「問到立場不表態」的——沒有成品可給，品質守門也擠不出東西來。**基準數字是使用者提供的實務值，不要自己改也不要自己補新的數字**（到店率 20%、回流率 30-40%、燙染對話成本 100/150/200/250、接髮 350/500、漲價 5-10%）；客單價與指名率刻意不給基準，因為它們沒有通用值，只能跟自己比。
- **兩大主題**：每塊知識都屬於 `domain`＝`operations`（店務營運管理）或 `coaching`（設計師一對一行銷輔導）。資料列自帶 `domain` 優先，沒帶時由 `app/domains.py` 依分類與來源推斷；後台總覽、篩選與新增知識都以這兩個主題為軸。
- **知識治理**：匯入強制 `review_status=approved` + `access_level` 相符 + `rag_allowed=true`；任何一筆不合格整批拒絕。
- **HTTP/1.1 與串流收尾**：`app/server.py` 的 Handler 明寫 `protocol_version = "HTTP/1.1"`（預設的 1.0 沒有 keep-alive，每個靜態檔都要重開連線＋重做 TLS，畫面會變成「HTML 出來了但 CSS 沒有、一直轉」），`request_queue_size = 128`（預設 5 會被一頁的靜態檔塞爆）。代價是**每個回應都要讓瀏覽器知道 body 到哪裡結束**：`/api/chat/stream` 沒有 `Content-Length`，所以它自己送 `Connection: close` ＋ `close_connection = True`。新增任何不帶 Content-Length 的回應時務必比照辦理，否則游標會一直轉。`tests/test_api.py` 的 `ConnectionTests` 守著。
- **安全**：全部 SQL 參數化；回應含安全標頭與 HTML CSP（無 inline script）；POST 檢查 Origin；月預算超標即停用模型生成；聊天與登入均有限流。
- **零依賴（一個例外）**：不要引入第三方 Python 套件（Playwright 只用於本機測試）。唯一例外是 `psycopg`：只在 Dockerfile 安裝、只在設定了 Postgres 連線時 import（`app/replica.py` 守門），本機開發不需要它。
- **持久化走 Postgres 快照，不掛 Volume**（使用者決定）：SQLite 是可拋棄的工作庫；`app/replica.py` 把 users／sessions／audits／feedback／自訂知識壓成 gzip JSON 單列快照存 Postgres，開機還原、定期備份（內容沒變不上傳）。改 durable 資料表 schema 時記得欄位取交集的還原邏輯已涵蓋新增欄位，但刪欄位要同步看 `apply_snapshot`。
- **health check 標記**：`app/health.py` `_frontend_check` 會驗證前端檔案內含特定字串（如 `.chat-main`、`id="admin-shell"`、`/api/chat`）；改前端時勿移除。
- **閒聊不進檢索**：打招呼／道謝／應聲、**自我介紹**、抒發情緒、欲言又止這四類在檢索前就被 `app/policy.py` 攔下來（`smalltalk()`／`emotion_only()`），交給 `AnswerEngine.smalltalk()` 讓模型自然接一句，不掛來源。情緒句只承接、不派任務也不要數字；句子裡有「怎麼／該不該／什麼／嗎」這類提問字就讓路給 RAG。`tests/test_smalltalk.py` 守著分流。
- **規則正本只有一份**：三種語氣的規則住在 `app/tuning.py` 的目錄，`app/answer.py` 的 `TONE_INSTRUCTIONS` 只是用 `compose_tone()` 組出來的衍生值。**不要改 `TONE_INSTRUCTIONS`，改了不會生效**（`tests/test_tuning.py` 會擋）。
- **AI 模型校調（後台唯一改規則的地方）**：所有送給模型的規則都在 `app/tuning.py` 的目錄裡——基本回答規則（`config/designer_coach_policy.md` 依 `## ` 切段）、三種語氣、固定回覆句，共 44 條。後台 `#tuning` 分頁逐條顯示與編輯，改過的存進 SQLite `model_rules`（已納入 Postgres 快照），沒改的用預設；`AnswerEngine.instructions()` 每次組指令時重讀，存檔後下一則就生效。**改規則請改後台或 `tuning.py` 的預設值，不要再直接改 `TONE_INSTRUCTIONS`**（它已改由目錄組出來）。`tests/test_tuning.py` 會確認沒有任何覆寫時組回來的字串跟原本逐字相同。
- **上傳分析不留檔案**：後台「新增知識」是拖檔進來 → `POST /api/admin/knowledge/analyze` 分析成候選 → 人逐塊確認改寫 → 按儲存才寫進 `chunks`。**檔案本身不存**，只存萃取出來的知識；一次送一份檔案，前端才能一份一份顯示進度條。**可以讀 Word／Excel／PowerPoint／PDF／RTF 與各種文字檔**——新版 Office 是 ZIP 裡放 XML，標準庫的 `zipfile`＋`xml.etree` 就拆得開，所以沒有破零依賴。舊版 `.doc/.xls/.ppt`（OLE 複合文件）與掃描版 PDF 讀不了，錯誤訊息要直接講「請另存成 .docx」而不是「讀取失敗」。這條端點的請求上限單獨放寬到 8MB（二進位檔走 base64 會再放大 4/3）（一般是 64KB＝約兩萬中文字，文件一定超過），**其他路徑不可以跟著放寬**（`tests/test_api.py` 守著）。模型不通時 `extract.split_document` 用規則切，本機沒有 API key 也能用。
- **健康檢查不可以做有副作用又要拿鎖的事**：`_persistence_check` 原本每次都呼叫 `replica.backup()`，而 `export_snapshot` 會在 `store._lock` 裡把所有 durable 表讀出來，導致後台知識庫分頁卡在「載入中」（health 與 chunks 是同時發的）。現在只做 `replica.probe()`（讀快照的時間與大小），備份交給背景執行緒，失敗時看 `replica.last_error`。
- **後台定位**：知識編輯台（總覽／知識庫／品質檢查／帳號／系統健康）。知識庫是 QA 式收合清單（一列一則、點標題展開），清單只帶前 400 字，展開時才抓完整內容。可直接新增編輯知識，存 SQLite（`chunks.origin='custom'`），重建索引不會被覆蓋；`匯出 JSONL` 可下載回存 repo 永久化。
- **對話紀錄存伺服器**（2026-09-01 使用者決定改掉原本只存瀏覽器的做法）：`conversations` 表（per user id）＋`user_prefs`（語氣偏好），兩張都在 `DURABLE_TABLES` 裡，換裝置與重新部署都看得到。localStorage 降為離線快取；**每次進入網站（`bootstrap` → `restoreSession`）與分頁回到前景時**都會拉伺服器那份合併（伺服器為主）。兩個不能拆掉的守則：(1) **已同步過之後，「伺服器沒有、本機有」＝在別台刪掉了，不可以再推回去**（推回去會讓刪掉的對話復活），只有上次同步之後才新建的才推，靠 per 帳號的 `lastSyncAt` 標記分辨；(2) **關閉分頁時只有在本機真的有未送出的修改（`pendingPush`）才補送**，否則會用這台的舊版本蓋掉別台剛存的新版本，舊使用者只在瀏覽器裡的紀錄會在第一次登入時自動搬上去。每個帳號最多留 100 段、每段 200 則、單則 2 萬字（`app/server.py` 的 CONVERSATION_* 常數）。稽核仍然照舊另存（問題已遮罩 PII）。

## 工作流程

- 分支：`claude/codex-review-optimization-sw0j31`（或當前指定分支）→ push → **開 PR 併入 main（使用者已授權每次 push 後自動 merge）**。main 一更新 Zeabur 就自動部署。
- push 前：`python3 -m unittest discover -s tests` 必須全綠；UI 改動用 Playwright 截圖驗證（chromium 在 `/opt/pw-browsers/chromium-1194/chrome-linux/chrome`）。
- commit 訊息、PR、程式註解不得出現模型名稱。
- 正式環境除錯：Zeabur Log 搜 `[boot]`（開機摘要：profile／chunks／db／model）與 `[llm]`（模型失敗階段與原因，不含金鑰與問題內容）。
