# Task8 檢索相關性與無答案保護

Commit: `f840570bf41e64fd43122c009552519cc71bafda`（基底 `985000a`）。工作目錄 `/workspace/scratch/8a3014a37e18/lureai-fixes`。未 push、未做 paid/live 呼叫、未讀取父代理另外凍結的 30 題。

## 結果與界線

本次修正已通過下列 scoped 相容性檢查；既知 20 題的 no-answer 誤答從 4/5 降為 0/5，但 answerable Recall@3 仍為 0.4000，不能宣稱所有改寫都能找對來源，也不能把種子題 100% 覆蓋率當成泛化品質。後續獨立評測與 Task6 全套 gate 由父代理執行。

| 指標 | 修正前 | 第一輪來源支持修正 | 最終通用疑問詞修正後 |
| --- | --- | --- | --- |
| Answerable cases | 15 | 15 | 15 |
| No-answer cases | 5 | 5 | 5 |
| Recall@3 | 0.4000 | 0.4000 | 0.4000 |
| MRR@3 | 0.2555556 | 0.3333333 | 0.3333333 |
| Raw score >= .72 no-answer proxy | 0.8000（4/5） | 0.2000（1/5） | 0.0000（0/5） |
| PolicyEngine no-answer false positive | 0.8000（4/5） | 0.2000（1/5） | 0.0000（0/5） |
| 574 個核准 seed 的 Recall@3 / 過門檻比例 | 1.0000 / 1.0000 | 1.0000 / 1.0000 | 1.0000 / 1.0000 |

**20 題已是觀察過的診斷／回歸資料，且其殘餘失敗確實促成最後一項通用修正。最終分數不是新盲測。** 沒有改 gold locator、fixture、手冊、編譯知識、question_bank、aliases 或 synonyms。

## 實作

- `app/retrieval.py` 保留 FTS candidate retrieval 與 SearchHit 介面，以來源實際標題／段落／內文的支持覆蓋率和語料特異性取代 aliases 混合 search_text 的無條件 overlap 加分。aliases 和 curated 身分的增益乘上來源覆蓋及特異性，不能自行形成高信心；核准 aliases 整句或 source title/section/text 完整命中仍保留強證據。
- 每份文件對 term frequency 只投一票；aliases 的頻率只作反向的共用模板折扣，從不提供正向 source support。原查詢 CJK bigram／Latin word 映射回字元位置，每個位置只計一次；未支持主題保留 rare-term 等級的權重，不把未知詞從分母丟掉。原有同義詞可提供等義 source 支持，沒有新增 vocabulary mappings。`〔已遮罩〕` 是傳輸遮罩標記，不算未知主題。
- 問句連接語與數量疑問詞在建立證據 bigram **之前**先移除，避免「漲多」這種跨到「多少」的片段被當作罕見主題。這是通用語法處理，不是離題領域清單。
- 分數依然用既有 compression；0.72 是操作門檻，不是概率。PolicyEngine、CustomerService、FollowupPlanner 等消費者收到同一種分數，沒有只在單一路徑另外遮蔽錯誤。
- `KnowledgeStore.retrieval_snapshot` 在鎖內取來源快照。local knowledge revision 涵蓋自訂新增／更新／刪除／清空／reindex，SQLite data_version 涵蓋其他 connection 的提交；同 connection 的 audit/accounting 寫入不導致重新斷詞。沿用已治理的索引與既有 access/import 約束，沒有擴大可查閱 corpus。
- `CustomerService._route` 不再把所有短句或沒有沙龍名詞的句子視為接話。明確省略主題的指代／改寫保留歷史；自足問題須先有自己的支持，補歷史也只能採用當題原已支持的來源。補歷史不能把 .74 的新主題換成 .95 的不同舊主題。一般／串流共用分流，三種 tone、accounting、expert citation guards、LINE 與自訂規則未另建旁路。
- `brain.md` 同步新的來源支持及脈絡界線。

## 殘餘案例如何發現並修正

第一輪來源特異性修正後，n04「台積電下週股價會漲多少」仍以 0.7534 取到 `career-23`「一次漲多少才不會嚇跑客人」。沒有 synonym expansion；實際 matched source terms 只有「漲多」「多少」。原位置 coverage 和 field coverage 都是 0.2367955，specificity 0.9225390，乘 specificity 前 evidence 0.2912585。來源中的罕見疑問述語被錯認為 subject，儘管「台積電／股價」都沒有支持。

父代理審定這是需要修的 gate defect，沒有把它包裝成可接受品質。新增不同主題的「玄武岩密度會升多少／陀螺儀轉速會降多少」合成測試，來源分別是育苗肥料與水槽液位；兩題在修正前均錯誤 answer。通用的 pre-tokenization 疑問詞處理後均拒答，且短實質主題、source exact match 正向控制仍通過。之後重新量測既知 20 題得到上表最終結果。

相關證據：`task8-no-answer-detail.log`、`task8-residual-evidence.log`（均為**第一輪**殘餘診斷，不是最終分數）、`task8-quantity-red.log` 與 `task8-final-diagnostic.log`。

## TDD 與驗證

初次 `python3 -m unittest tests.test_retrieval_support -v`：7 tests，14 項 assertion failures，包含 shared-boilerplate admission、custom 新來源輸給模板，以及 normal/stream × expert/service/line 的歷史借題誤答；正向 short query／approved alias／synonym／dependent followup 先保留。`task8-red.log`。

追加「新問題已有弱支持，不得被另一份舊來源取代」：1 test 的 normal/stream 兩項預期 red；`task8-context-red.log`。追加不同未知 subject 共用罕見數量述語：1 test 的兩項預期 red；`task8-quantity-red.log`。最終 synthetic 檔共 10 tests，另涵蓋 warm/fresh corpus consistency、custom clear、另一 SQLite connection 的失效，以及短 subject／source exact 正向控制。

初次較廣檢查有一個本次造成的 masked-contact admission failure（遮罩與未匹配文字在小語料被過重計入），另有一個我誤填不存在 `tests.test_followups` 的 loader error。沒有調低測試或刪掉 assertion；修正 source normalization/OOV weight，改用存在的 `tests.test_followup_chain` 後綠燈。原失敗保留於 `task8-scoped.log`。

最終 scoped 命令與結果：

```text
python3 -m unittest tests.test_retrieval_support tests.test_retrieval_policy tests.test_service tests.test_followup_chain tests.test_welcome_prompts tests.test_storage tests.test_ingest tests.test_bot_api -q
Ran 116 tests in 34.353s
OK
```

`task8-final-scoped.log`。其中包含 3 條各 50 輪的 followup chains；不是線上模型會話評分。

`python3 scripts/coverage_report.py`：574 questions，hit_rate 1.0，above_threshold 1.0；`task8-before-seeds.log` 與 `task8-final-seeds.log`。`python3 scripts/evaluate_holdout.py` 前後結果為表列值；`task8-before-diagnostic.log`、`task8-after-diagnostic.log`、`task8-final-diagnostic.log`。`git diff --check`、修改 Python 檔的 `py_compile` 均通過。

## 尚未證明的事

- Lexical specificity/coverage 不是語意蘊含或真正的主詞解析；仍可能把同一罕見 action 或 polysemy 誤當同主題，也可能拒絕未在同義詞層表示的合法改寫。
- 既有 FTS 仍只取有限候選，再以新 evidence 排序；不保證所有 answerable paraphrase 的正確來源進入候選。已量測 Recall@3 = 0.4 說明這個限制不能略過。
- 真正依賴脈絡但不符合保守指代／改寫語法的說法，可能要求補充而非借用舊題；自足問題不再能只靠歷史跨過 gate。
- 沒有增加外部服務或 embedding，沒有付費模型答案、LINE 真人／production 測試或全套 Task6 結果。最終 0/5 僅描述此已知診斷集，不宣稱真實世界零誤答或整體 100 分。

## Review fix round 1：alias-only 主題不能縮小分母

審查 Important 1 已以最小來源支持修正處理；本段取代上文「aliases 頻率只作反向折扣」尚未真正成立的實作描述。基底 `f840570bf41e64fd43122c009552519cc71bafda`，沿用中斷代理留下的兩條 synthetic regression，未新增／修改正式 aliases、synonyms、knowledge 或 gold。

- `_source_frequencies` 現在快取兩份每文件一票的 DF：實際來源 DF 用於原問題權重、未知主題質量與同義詞已知詞判斷；source+alias template DF 只用於正向 specificity 折扣。因此只在 aliases 出現的主題不能降低原問題未匹配字元的分母權重。
- 第一個實作只拆 DF，12 條 synthetic 測試仍有同樣兩條失敗：unsupported alias matches 還會增加乘 coverage 的 alias bonus。最終將非整句 alias 加分限定為 query、alias 與實際 source 三者共有的詞，堵住同一 alias-only 注入的另一個正向貢獻。核准 exact alias 仍有排序優先與 coverage/specificity=1 的信任例外；既有 short/source exact/synonym 正向控制保持可答。
- 同步 `brain.md` 的 DF 邊界，並修正 review Minor 指出的 `app/service.py` 註解及 `CLAUDE.md` 過期 followup 說明：明確省略主題的指代／改寫才是接話，短句或沒有沙龍名詞本身不構成判斷。

TDD red（生產碼修改前）：

```text
python3 -m unittest tests.test_retrieval_support.SourceSupportTests.test_partial_aliases_cannot_erase_unsupported_original_subject tests.test_retrieval_support.SourceSupportTests.test_nonexact_alias_frequency_cannot_promote_an_unrelated_cost_source -v
Ran 2 tests in 0.017s
FAILED (failures=2)
```

兩者皆在追加非整句 aliases 後 `assertNotEqual(policy.evaluate(after).action, 'answer')` 失敗，before rejection 先通過。只拆 DF 的中間版 `python3 -m unittest tests.test_retrieval_support -v`：12 tests / 2 failures（同兩條）。最終同命令：12 tests in 0.101s / OK。

Reviewer 的固定六來源 probe：row-1 body=`燙髮 估算成本`、query=`太陽系行星軌道週期長期變化估算成本`；每列追加 `太陽系行星軌道週期長期變化有哪些步驟` 並重新匯入，來源本身完全相同。修正前審查證據為 **0.7063 escalate → 0.9586 answer**；最終實跑為 **row-1 0.7063 escalate → row-1 0.7063 escalate**。另一條不同 unsupported subject 回歸也檢查追加 aliases 後分數不得增加。

Focused green：

```text
python3 -m unittest tests.test_retrieval_support tests.test_retrieval_policy tests.test_service tests.test_followup_chain tests.test_welcome_prompts -q
Ran 78 tests in 22.445s
OK
python3 scripts/coverage_report.py
{"questions": 574, "hit_rate": 1.0, "above_threshold": 1.0}
```

證據保存在 `task8-review-fix-scoped.log` 與 `task8-review-fix-seeds.log`。`tests.test_docs_contract` 另驗證同步文件；修改的 Python 檔 `py_compile` 與 `git diff --check` 通過。沒有重跑 broad suite 或已知20診斷，亦沒有讀 private-fresh-evaluation.json、私有新評測、付費／live 呼叫或 push。此次只證明 named alias-frequency defect 及上述相容性，先前 Recall@3 殘餘限制仍然有效；Task9 的私有即時資訊／medical/legal 邊界另行處理。
