# Assessment Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task.

**Goal:** 修正完整評估 F01–F12，交付可驗證程式與驗收紀錄。

**Architecture:** 保留 stdlib + SQLite FTS5 + vanilla JS；Postgres 快照加入可靠寫入協定。同步改為明確版本與 ACK，模型改為可觀測且有上限的呼叫生命週期。

**Tech Stack:** Python 3.11+、SQLite、psycopg、JavaScript、unittest、Node、Playwright。

**Spec:** docs/superpowers/specs/2026-09-05-assessment-design.md

## Global Constraints

- 繁體中文 UI；保留 expert/service/line 與 lurebot API。
- LINE 不送降級來源文字；沒有可調停頓面板。
- 知識數字與手冊不任意修改；不加入 Volume。
- 不得在註解或 commit/PR 出現模型名稱。
- 不得把真實模型評測、正式部署驗證或 MFA 外部設定標成已完成。
- 每項先有失敗回歸再實作；不推送、不合併，由主代理整合。

### Task 1: 快照可靠性 F01/F08

**Files:** app/replica.py、run.py、app/health.py、tests/test_replica.py、tests/test_runtime.py；必要時新增 tests/test_replica_integration.py。
**Interfaces:** PostgresReplica.restore/start/backup/stop 保留可呼叫介面；將資格、single-writer fencing、history 封裝其中。新 durable table 自動或明確包含後續同步／用量表。

- [ ] 寫還原失敗後不得備份、兩個寫入者不能互蓋、歷史快照可保留、停止與背景備份不競態的測試。例如 `with self.assertRaises(...): replica.backup(store)` 在 restore 失敗後。
- [ ] 執行 `python3 -m unittest tests.test_replica tests.test_runtime -v`，確認新測試失敗原因。
- [ ] 實作 fail-closed 還原、首次部署初始化、多版本快照與寫入者 fencing/lock。釋放 writer 前完成最後備份；無法取得 writer 的實例不得提供可寫服務。加入 SIGTERM 安全關閉及 bounded DB timeout。
- [ ] 重跑前述測試，補真實 Postgres integration 可選測試；提交。

### Task 2: 對話同步 F02/F03/F04

**Files:** app/storage.py、app/server.py、static/chat.js、tests/test_conversations.py、tests/test_sync_timing.py、tests/test_storage.py、tests/test_replica.py。
**Interfaces:** 儲存回傳逐筆 ACK（id、rev、accepted/conflict/deleted）；GET 同步回墓碑；既有 owner 邊界保持。伺服器拒絕同 rev 不同內容與舊副本復活。

- [ ] 新增相同 rev 不同內容、刪除後重送、HTTP 200 卻拒絕項目不可清 dirty、多裝置衝突內容不得丟失測試。
- [ ] 執行 `python3 -m unittest tests.test_conversations tests.test_sync_timing tests.test_storage -v` 確認失敗。
- [ ] 實作條件寫入、持久墓碑、逐筆 ACK 及前端衝突處理；同內容同 request 可冪等；保留未確認內容與離線恢復。
- [ ] 回歸、`node --check static/chat.js`，提交。

### Task 3: 模型生命週期與配額 F05/F06/F07

**Files:** app/answer.py、app/service.py、app/server.py、app/storage.py、app/usage.py；可新增 app/model_runtime.py 或 app/budget.py；tests/test_answer.py、tests/test_service.py、tests/test_usage.py、tests/test_api.py。
**Interfaces:** 現有 answer tuple 相容，串流明確 terminal 狀態；用量在中斷仍留 durable 記錄；未知用量不得當零。固定上限與設定有限值驗證。

- [ ] 新增 incomplete、EOF、disconnect、預留不足、結算競態、全域併發／429 cooldown 的失敗測試。
- [ ] 執行對應 unittest 確認 red。
- [ ] 抽出共用模型呼叫控制：有限輸出、端到端 deadline、每個模型呼叫用量追蹤、失敗原因；全域同時生成限制／RPM／TPM 與 429 冷卻。所有生成路徑包含 title、smalltalk、extract 與重試。
- [ ] 預算原子讀取檢查預留，涵蓋使用者與系統；未知用量保守結算，取消時關閉 generator/response。前端未完成狀態可重送，LINE 不送 partial。
- [ ] 回歸並提交，記錄未知帳單與供應商費率限制。

### Task 4: 帳號防護 F09

**Files:** app/auth.py、app/server.py、static/admin.html、static/admin.js、tests/test_auth.py、tests/test_api.py、tests/test_runtime.py。
**Interfaces:** 新建/重設最低 15 字及常見弱密碼阻擋；既有密碼登入及 bootstrap 已存在帳號相容。登入 attempt 在驗證前佔位避免併發穿透，有全域／IP 上限，不能因未知帳號高速字典攻擊無界增長。

- [ ] 新增弱密碼拒絕、舊帳號部署相容、併發登入限制測試，跑 red。
- [ ] 實作長度／弱密碼檢查與有界登入驗證，更新 UI 和測試 fixture 的新密碼；不增加非必要 MFA 自製協定。
- [ ] 跑 auth/api/runtime 測試並提交；企業登入或 MFA 作為需要外部設定的明確待驗收項。

### Task 5: 引用診斷與獨立評測 F10/F11

**Files:** app/answer.py、app/service.py、app/quality.py、scripts/evaluate_holdout.py、tests/fixtures/rag_holdout.json、tests/test_holdout.py、tests/test_citation_recovery.py、brain.md。
**Interfaces:** 保留三語氣契約；新增 citation coverage／numeric support 診斷與 quality_failed 訊號；獨立資料不進索引與 aliases。

- [ ] 新增不存在引用、重要數字無來源、重試仍品質不足的測試並先看失敗。
- [ ] 實作保守診斷，不將 regex 當語意證明，不讓正常聊天無故降級。建立未參與 aliases 的自然改寫／無答案集，檢查無 exact-seed 重疊。
- [ ] `python3 scripts/evaluate_holdout.py` 報 Recall@3、MRR、無答案誤答率；真實成績如實輸出，不調索引迎合測試。
- [ ] 建立 50 段多輪人工／模型評測資料與 runner（預設離線；真實 API 顯式啟用），更新 brain.md，回歸並提交。

### Task 6: 部署驗證與文件 F12

**Files:** .github/workflows/ci.yml、Dockerfile、requirements*.txt、README.md、CLAUDE.md、MEMORY.md、docs/operations.md、tests/browser_smoke.py。
**Interfaces:** CI 跑完整 unittest、JS、RAG holdout、Postgres integration、browser smoke；產物留 evidence。固定依賴版本、非 root 容器、可重建設定；不聲称分支規則已啟用。

- [x] 更新過時知識數量、repo 路徑及安全／同步／快照／模型設定文件。
- [x] 建立依賴固定、CI 與瀏覽器驗證；加入啟停／備份復原／帳單对帳／負載測試操作說明。
- [x] 執行本機可用的完整測試與逐檔 JS 語法；瀏覽器 smoke 與 real Postgres 配進 CI，並記錄本機限制。
- [x] 完成 F01–F12 修正對照表與剩餘外部驗收，提交後進入全分支審查。

### Task 7: 對話事實與規則遷移延伸

- [x] 修正數值型別、目前／歷史零狀態、客用成品事實與敏感結論。
- [x] 規則遷移版本存入 durable `rule_state`，由 `tuning.rule_versions(store)` 讀取。

### Task 8: 通用檢索來源支持延伸

- [x] aliases 只協助召回；來源實文支持、特異性與原題覆蓋共同決定 admission。
- [x] 限制跨題脈絡借用並保留明確省略主題的合法接話。

### Task 9: 即時資料與敏感結論邊界延伸

- [x] 未提供的店內即時價目／排程／顧客資料在生成前攔截。
- [x] 醫療確診與法律權利結論在生成前攔截，保留安全溝通與已提供資料改寫。
