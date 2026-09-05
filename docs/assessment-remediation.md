# 全面評估修正工程紀錄

本紀錄對應 2026-09-05 的 F01–F12 與後續核准 Task 7–9。狀態只表示程式與本機回歸已完成；外部系統驗收另列，不以文件代替證據。

| 項目 | 修正 | 程式狀態 | 尚需外部驗收 |
| --- | --- | --- | --- |
| F01 | Postgres restore fail closed、單 writer lease、斷線 fencing | 完成 | 正式 Postgres 故障演練 |
| F02 | 對話以 revision 條件寫入 | 完成 | 多裝置正式流量觀察 |
| F03 | tombstone 與衝突副本防止刪除復活／覆寫 | 完成 | 同上 |
| F04 | 逐筆 ACK 保留未確認 dirty 狀態 | 完成 | 同上 |
| F05 | 每個生成呼叫 durable reservation/settlement，未知費用不歸零 | 完成 | 供應商帳單對帳 |
| F06 | 原子預算硬上限與 process-wide concurrency/RPM/TPM/cooldown | 完成 | staging 負載、供應商帳單與實際有限預算 |
| F07 | 有限輸出、共同 deadline、incomplete/EOF/disconnect 終態 | 完成 | 真實模型及斷線驗收 |
| F08 | 快照 history、SIGTERM 最後備份、writer eligibility | 完成 | 正式還原演練與 retention 執行 |
| F09 | 新建／重設密碼門檻與有界登入 admission | 完成 | 企業 MFA/SSO 設定 |
| F10 | 引用編號、重要數字來源支持與 final-output diagnostics | 完成 | 真實回答人工審閱 |
| F11 | 獨立離線 holdout、MRR@3 與 no-answer policy 指標 | 完成 | 新的未揭露資料與付費 50 組測試 |
| F12 | 固定依賴、非 root 映像、CI、瀏覽器/Postgres jobs、操作手冊 | 已配置 | CI 實跑、branch rules、映像建置及正式部署 |
| Task 7 | 客用成品 typed facts、時序零狀態、敏感結論、durable rule version | 完成 | 新 50 組未做付費模型測試 |
| Task 8 | generic source support、alias 邊界、自足新題不得借歷史過門檻 | 完成 | 未揭露語意改寫集；Recall@3 仍有限 |
| Task 9 | 即時店務資料、醫療確診、訂金法律結論生成前攔截 | 完成 | 新意圖措辭的持續人工抽查 |

## 評測證據界線

- 舊 main 的 40 組 live 與 10 組 LINE replay 是既有報告來源，不是本分支重跑。
- Task 5 新增的 50 組為依問題重建的 fixture，不是原始逐字稿，也沒有在本分支付費實跑；Task 7 以其中揭露的問題補強事實與規則邊界。
- 已知 20 題在最終分支回歸為 Recall@3 `0.4000`、MRR@3 `0.3222222`、no-answer threshold proxy `0/5`、policy false positive `0/5`；它已參與診斷及修正，只能稱回歸。
- 父任務在 `f840570` 前凍結的 30 題首次結果為 Recall@3 `0.45`、MRR@3 `0.425`、no-answer threshold proxy `4/10`、policy false positive `4/10`。四個失敗已揭露並用於 Task 9，之後只能稱 regression；fixture SHA-256 為 `b2ddfd30b3f39199946c490dc3678f19d69c068760beda893429202bbeb102b0`。
- 最終分支對同一 frozen 30 題的回歸為 Recall@3 `0.45`、MRR@3 `0.425`、raw threshold proxy `3/10`、policy false positive `0/10`。這是揭露後 regression，不是 post-fix blind score。
- 沒有真實模型、真人 LINE、正式部署、MFA 或 production billing 測試。不得從離線 lexical gate 推論整體語意正確率或滿分。

## 交付與來源

CI action 版本依 2026-09-05 官方 GitHub release 查核：checkout `7.0.1`、setup-python `7.0.0`、upload-artifact `7.0.1`。Python 依賴依官方 PyPI 查核固定為 psycopg `3.3.5`（Python 3.10+）及 Playwright `1.62.0`；vulnerability scanner 固定為 pip-audit `2.10.1`。本機安裝的是較早 psycopg，沒有把它稱為最新版本。

CI 產物保留 unittest、逐檔 Node syntax、離線 holdout、real disposable Postgres integration、browser smoke 截圖／console 與 dependency inventory/consistency audit。必要工作是否成為 branch gate 仍需倉庫管理者設定並驗證。
