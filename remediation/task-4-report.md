# Task 4 帳號防護驗收紀錄

## 完成內容

- 新建與重設密碼最低長度改為 15 字，最長 256 字，並拒絕常見弱密碼的大小寫等價字串。
- 登入不套用新密碼規則；既有短密碼雜湊仍可驗證。
- bootstrap 先依帳號查詢既有資料；帳號存在時不驗證或雜湊環境變數中的舊密碼，也不重寫帳號。
- 登入在 scrypt 前，以同一把鎖原子預留 account、IP、global 三個範圍。
- 同時進行的密碼驗證固定上限為 4；限流追蹤 key 固定上限為 4096，額滿時先清除過期紀錄，仍額滿則拒絕新嘗試。
- account 預設 5 次、IP 20 次、global 100 次失敗上限。成功登入只清除該 account 的失敗紀錄，保留 IP 與 global 攻擊歷史。
- 後台密碼欄同步標示並以 HTML 約束 15 至 256 字；README、環境範例、replica/storage/browser 測試 fixture 同步更新。

## Red / Green 證據

實作前執行：

```text
python3 -m unittest tests.test_auth
Ran 14 tests in 4.391s
FAILED (failures=2, errors=6)
```

失敗涵蓋最低 15 字、常見弱密碼、既有帳號 bootstrap、原子 reservation、驗證併發上限、成功登入保留 IP/global 歷史，以及限流儲存上限。

實作後重點回歸：

```text
python3 -m unittest tests.test_auth tests.test_api.ApiTests.test_successful_login_keeps_ip_failure_history tests.test_api.ApiTests.test_concurrent_login_verification_is_bounded_before_auth_work
Ran 16 tests in 6.080s
OK
```

## 驗證結果

```text
python3 -m unittest tests.test_auth tests.test_api tests.test_runtime
Ran 82 tests in 49.855s
OK

python3 -m unittest tests.test_replica tests.test_storage tests.test_static_ui
Ran 84 tests in 2.384s
OK

git diff --check
exit 0
```

## 尚待外部驗收

- 企業身分登入與 MFA 需要外部身分服務及正式環境設定，本次未自製協定，也未標記為完成。
- 未執行正式部署、付費服務、真實模型或瀏覽器端到端測試。瀏覽器 smoke 的新帳號 fixture 與靜態 DOM 契約已由 `tests.test_static_ui` 驗證。
