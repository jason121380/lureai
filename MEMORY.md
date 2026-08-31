# MEMORY.md — 決策與偏好紀錄

跨 session 的專案記憶。新的決策往上加，帶日期。

## 工作流程偏好（使用者指定）

- **每次 push 後一律開 PR 併入 main**（2026-08-31 授權），main 更新後 Zeabur 自動部署。
- 測試必須全綠才能 push；UI 改動用 Playwright 截圖驗證後再交付。
- Repo 已改名 `jason121380/lureai`（舊名 hair_brain 自動轉向）。

## 產品決策

- **2026-08-31 知識庫大整理**：移除 490 個對話逐字稿與 788 個 OCR 碎片，改由原始對話萃取成 41 塊乾淨策展知識（coach-01~41）。實測 23/23 題可回答，其中 20 題由策展知識回答。原始 JSON（270 對話 12,873 則）中僅 1,810 則為教練實質建議，其餘為數據回報樣板與短句。
- **2026-08-31 檢索三修**：(1) `config/synonyms.json` 同義詞層——原本「一週幾則」與「每週發布頻率」字面零交集完全撈不到；(2) 策展知識加權 +0.10 與標題聚焦分，避免短 SOP 被長逐字稿壓過；(3) 分數改用單調壓縮（0.5+0.5(1-e^-2.63x)）取代硬截斷，修正 41 塊同時撞 1.0 導致排序退化成字母序。
- **2026-08-31 後台改為知識編輯台**：移除檢索測試與查詢稽核；改為 總覽／知識庫／品質檢查／帳號／系統健康。目的是讓回覆資料整齊、多元、完整、可擴充、有條理。

- **2026-08-31 移除客服版**：只保留 `designer_coach` profile；客服知識檔、customer_policy.md、start.command 已刪除，預設 profile 改為 designer_coach。
- **2026-08-31 關聯問題**：模型在同一次生成結尾以「▷ 」行輸出 3 個追問，service 解析為 `followups`，前端顯示為可點選項。
- **2026-08-31 輸出無上限**：預設不設 max_output_tokens（`LLM_MAX_OUTPUT_TOKENS` 可選擇性限制）。

- **2026-08-31 統一登入**：廢除管理權杖登入頁。所有人走同一個帳號登入；admin 角色帳號在側欄看到「設定」icon（僅 admin 可見）→ 進 `/admin`；非 admin 開 `/admin` 直接導回 `/`。`ADMIN_TOKEN` 只保留給 API header（curl／測試／緊急）。
- **2026-08-31 角色制**：帳號分「一般用戶 user／管理者 admin」；後台建帳號時選權限；`USER_ROLE` 可指定 bootstrap 帳號權限。
- **2026-08-31 密碼門檻**：最低 4 個字（使用者要求，接受安全取捨；scrypt + 登入限流仍在）。
- **2026-08-31 全站視覺**：複製 ChatGPT 風格（見 STYLE.md）。對話：使用者訊息右側灰氣泡、AI 左側純文字、無頭像無角色標籤。
- 移除項目：側欄使用者名下的應用名稱副標、右上角連線狀態圈圈、後台頂部「知識庫管理＋管理權杖」列、後台的權杖輸入 UI。
- 用量面板做成獨立卡片，與帳號列視覺分離。
- 後台為分頁式導覽（hash 路由），進入後所有分頁資料自動載入，不需任何手動觸發。

## 技術決策

- 零第三方依賴維持不變（stdlib + SQLite FTS5）。
- SQLite 即正式資料庫，不上 PostgreSQL；Zeabur 需掛持久化 Volume + `APP_DB_PATH`，否則帳號／用量／稽核在重新部署時歸零（**尚未確認使用者已掛 Volume**）。
- 月預算（`MONTHLY_BUDGET_TWD`）為硬限制：超標自動停用模型、降級抽取式（`model_status: budget_exhausted`）。
- 聊天限流 `CHAT_RATE_LIMIT_PER_MINUTE`（預設 20/分）。
- LLM timeout `LLM_TIMEOUT_SECONDS`（預設 60；曾因 20 秒太短導致推理模型全數降級「模型暫時無法完成生成」）。
- scrypt 參數 N=2^15/r=8/p=4（OWASP 等效、單次驗證 ~32MB），驗證在鎖外執行、入庫前鎖內重驗 hash。
- 稽核 log 的問題欄位自動遮罩 PII。
- loading 佔位訊息不落地：載入 localStorage 時過濾 `loading` 訊息（修復「重新整理後永遠轉圈」）。

## 已知待辦／觀察

- 2026-08-31：聊天已改串流輸出（/api/chat/stream，ndjson delta+result；/api/chat 保留為相容端點）。
- 2026-08-31：修復去識別化過度遮罩——coach 區塊由 knowledge/designer_coaching_process.md 重建；export_deploy_knowledge.py 加入角色詞/縮寫停用表、knowledge/* 來源不再重遮罩。歷史教材/案例的 [人名][歷史數值] 屬隱私設計，完整版需在使用者 Mac 重跑匯出或掛私人 KNOWLEDGE_JSONL。
- 2026-08-31：登入頁極簡化為 LOGO + "Your Private Brain" + 表單。

- 生產環境曾出現 LLM 呼叫失敗降級（timeout 20s 時代）；調至 60s 後需實測確認。
- 歷史輔導案例 chunks 有少量雜訊（test/hi 對話、全遮罩片段，約 1-5%）；檢索已將歷史案例降權（最多附 1 筆），必要時可清洗 `knowledge/designer_coaching_process.jsonl`。
- 可考慮：回答串流輸出（SSE）讓長生成有即時回饋。
