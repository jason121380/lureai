# LUREAI 操作手冊

## 發布與啟停

發布前跑 CI 的四個必要工作：`unit-and-holdout`、`postgres-integration`、`container-build`、`browser-smoke`，並下載 evidence artifacts。GitHub rulesets 與 branch protection 必須另由倉庫管理者設定；本倉庫目前沒有證據可宣稱已啟用。正式映像建置時注入可追溯資訊：

```bash
docker build \
  --build-arg APP_BUILD_COMMIT="$(git rev-parse HEAD)" \
  --build-arg APP_BUILD_TIMESTAMP="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t lureai:release .
```

容器以 uid/gid 10001 執行，可寫路徑為 `/app/data` 與 `/app/qa`。不要把 SQLite 當跨部署持久層。啟動前設定 Postgres、帳號、有限預算與密鑰；`SYSTEM_MONTHLY_BUDGET_TWD=0` 是不限額，正式環境如需上限必須設正數。部署後查 `/api/admin/health`，核對 `build.commit`、`build.timestamp`、`rules.default_rule_schema_version`、`rules.override_migrated_version` 和 Postgres writer 狀態。

舊版本升級到 single-writer 協定前先停止所有舊實例，再啟動一個新實例。滾動期間不能讓舊 writer 與新 writer 同時服務。停止時送 SIGTERM 並等程序完成最後備份；若平台強制終止，下一次啟動後先查 health 與快照時間再開流量。

啟動時先取得 Postgres writer 並還原快照，再判斷環境變數帳號是否需要新建，最後執行規則遷移及啟動備份。已還原帳號保留原密碼雜湊、角色與停用狀態；舊的短 `USER_PASSWORD` 不會重設既有帳號，新帳號仍須通過 15 字及弱密碼檢查。還原或初始化失敗不啟動 HTTP，也不寫入替代快照。未設定 Postgres 時仍可使用本機 SQLite 呼叫記帳；已設定但無法取得 writer 時繼續拒絕呼叫。

## 快照備份與還原

Postgres 的 `lureai_snapshot` 是目前 head，`lureai_snapshot_history` 保留歷史版本。歷史目前不自動刪除；每月檢查資料量，先用 `pg_dump` 匯出兩張表到受控備份，再依組織保存期限封存。不要在線上直接刪除 history。

```bash
pg_dump "$DATABASE_URL" --data-only \
  --table=lureai_snapshot --table=lureai_snapshot_history \
  --file=lureai-snapshots.sql

psql "$DATABASE_URL" -c \
  'SELECT id, updated_at, octet_length(data) AS bytes FROM lureai_snapshot_history ORDER BY id DESC LIMIT 50'
```

還原演練必須用 disposable database。正式還原時先停服務並保存現有 head；在 transaction 中把選定 history row 複製到 head，commit 後只啟動一個實例。健康檢查正常且資料抽查通過後才恢復流量。保留被取代的 head，不要刪 live data：

```sql
BEGIN;
INSERT INTO lureai_snapshot_history(data, updated_at)
SELECT data, updated_at FROM lureai_snapshot WHERE id = 1;
UPDATE lureai_snapshot
SET data = (SELECT data FROM lureai_snapshot_history WHERE id = :history_id),
    updated_at = CURRENT_TIMESTAMP::text
WHERE id = 1;
COMMIT;
```

## 帳單對帳

process-wide concurrency/RPM/TPM 狀態會在重啟後歸零；`model_calls` 金額帳本會持久保存。供應商用量未知時仍保留完整預留費用。先停止服務並取得帳單證據，再執行：

```bash
python3 -m app.reconcile_usage --db /app/data/designer_coach.db list
python3 -m app.reconcile_usage --db /app/data/designer_coach.db \
  settle CALL_ID --cost-twd COST --reference INVOICE_REFERENCE
```

只有確認呼叫已終止但仍是 pending 時才加 `--allow-pending`。Postgres 模式會先取得 writer lease；取得不到就不可繼續。保留 invoice reference 和執行輸出作稽核證據。

## 容量與故障演練

負載測試只對 staging/disposable 資料執行。逐步增加並發，記錄 p50/p95/p99、HTTP 429/503、錯誤率、記憶體、SQLite lock、Postgres snapshot 大小與備份時間；同時驗證 `LLM_MAX_CONCURRENCY`、RPM/TPM、request deadline 和月預算拒絕。測試中斷串流、SIGTERM、Postgres 斷線與 writer lease 競爭，確認 partial 不會當成功、未知成本不歸零、失去 writer 後 fail closed。

正式外部驗收還包括真人 LINE reply-token 時限、實際模型權限與帳單、正式 Postgres 還原演練、企業 MFA/SSO、branch rules、平台部署及告警。這些不能由本機或 CI 假定完成。
