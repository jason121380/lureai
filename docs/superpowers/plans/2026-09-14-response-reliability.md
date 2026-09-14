# Response Reliability Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修復 40 題評估揭露的危急健康、錯誤拒答、生成失敗與延續型改寫問題。

**Architecture:** 在既有 RAG 前增加決定式危急分流，維持全域檢索門檻並用精準 aliases 與明確 follow-up 指代補召回。模型失敗時以 grounded source 產生最小可用 extractive 回覆，讓引用與失敗狀態都可稽核。

**Tech Stack:** Python 3.11+、SQLite FTS5、JSONL、unittest、vanilla JavaScript。

**Spec:** `docs/superpowers/specs/2026-09-14-response-reliability-design.md`

## Global Constraints

- 保留 Python 標準庫、SQLite FTS5、vanilla JS、expert/service/line 與 lurebot API。
- 不新增外部執行期依賴，不降低 `minimum_score=0.72`。
- 不做醫療診斷；危急分流只提供立即安全動作。
- 每一項正式程式修改前必須先看見對應測試以預期原因失敗。
- 修改知識 markdown 後重建 `knowledge/designer_coaching_process.jsonl`。
- 回覆行為改變時同步更新 `brain.md`。

---

### Task 1: 危急健康決定式分流

**Files:**
- Modify: `app/policy.py`
- Modify: `app/tuning.py`
- Modify: `app/server.py`
- Test: `tests/test_retrieval_policy.py`
- Test: `tests/test_acceptance_behavior.py`

**Interfaces:**
- Produces: `PolicyEngine.urgent_health(question: str) -> PolicyDecision | None`
- Produces: `reply-urgent_health` 固定回覆規則。
- Consumes: 現有 `PolicyDecision` 與 `PolicyEngine._override()`。

- [ ] **Step 1: 寫危急正例與邊界負例測試**

```python
def test_urgent_breathing_symptom_bypasses_rag(self):
    decision = self.policy.precheck("客人染後呼吸不順，還要先傳照片嗎？")
    self.assertEqual((decision.action, decision.reason), ("direct", "urgent_health"))
    self.assertIn("立即", decision.message)
    self.assertNotIn("傳照片", decision.message)

def test_non_urgent_redness_keeps_existing_coaching_route(self):
    self.assertEqual(self.policy.precheck("客人染後頭皮紅腫，怎麼回？").action, "continue")

def test_negated_or_educational_phrase_is_not_an_emergency(self):
    self.assertEqual(self.policy.precheck("貼文不要寫成讓人呼吸不順的長句").action, "continue")
```

- [ ] **Step 2: 執行測試確認以缺少 urgent_health 分流失敗**

Run: `python3 -m unittest tests.test_retrieval_policy tests.test_acceptance_behavior -v`

Expected: 新危急測試收到 `continue` 或 `grounded`，不是 `direct/urgent_health`。

- [ ] **Step 3: 實作最小危急分流與可調固定回覆**

```python
URGENT_HEALTH_MESSAGE = (
    "先不要等照片 也先停止目前的服務\n"
    "請立即聯絡當地緊急醫療服務 或由現場人員協助就醫\n"
    "我不能判斷原因 但呼吸或意識異常要先當緊急狀況處理"
)

def urgent_health(self, question: str) -> PolicyDecision | None:
    normalized = "".join(str(question or "").lower().split())
    if URGENT_HEALTH_NEGATION.search(normalized):
        return None
    if URGENT_HEALTH_SYMPTOM.search(normalized):
        return PolicyDecision(
            "direct", "urgent_health",
            self._override("reply-urgent_health", URGENT_HEALTH_MESSAGE),
        )
    return None
```

`precheck()` 必須在 `restricted_conclusion()` 和 `SENSITIVE_TOPICS` 之前回傳此決定。

- [ ] **Step 4: 重跑目標測試確認通過**

Run: `python3 -m unittest tests.test_retrieval_policy tests.test_acceptance_behavior -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/policy.py app/tuning.py app/server.py tests/test_retrieval_policy.py tests/test_acceptance_behavior.py
git commit -m "fix: add urgent health safety routing"
```

### Task 2: 延續型改寫與低召回問題

**Files:**
- Modify: `app/service.py`
- Modify: `config/question_bank.json`
- Rebuild: `knowledge/designer_coaching_process.jsonl`
- Test: `tests/test_coaching_rag.py`
- Test: `tests/test_followup_chain.py`
- Test: `tests/test_question_bank.py`

**Interfaces:**
- Consumes: `is_follow_up(question: str) -> bool`、`CustomerService._route()`。
- Produces: 明確格式／指代 follow-up 判斷，不改變自足新題目的隔離規則。

- [ ] **Step 1: 寫自然問法與多輪改寫失敗測試**

```python
def test_zero_data_beginner_reaches_starting_playbook(self):
    self.assert_top_locators_include(
        "完全沒記資料但覺得生意差，第一步做什麼？", "session-03"
    )

def test_formatting_follow_up_reuses_previous_subject(self):
    _hits, grounded, escalation = self.service._route(
        "整理成員工群組 80 字內清單。",
        [{"role": "user", "content": "做兩週沒改善，需要提供哪些數字？"}],
    )
    self.assertIsNone(escalation)
    self.assertIn("session-07", [hit.locator for hit in grounded[:3]])
```

另外為廣告點擊後私訊少、單變因測試、嫌貴、取消歸因、體驗價、客單下降、低價服務占比與零資料起步各新增兩個未照抄改寫案例，斷言正確 locator 在 top 3。接受來源依序是 `coach-16`、`coach-19`、`metric-07`、`career-24`、`session-03`，廣告與嫌貴題使用現有手冊中實際支持內容的 locator。

- [ ] **Step 2: 執行測試確認錯誤 locator 或 low_confidence**

Run: `python3 -m unittest tests.test_coaching_rag tests.test_followup_chain tests.test_question_bank -v`

Expected: 至少零資料起步與格式改寫案例 FAIL，輸出目前錯誤 locator。

- [ ] **Step 3: 最小擴充 follow-up 判斷與來源 aliases**

`is_follow_up()` 只接受含指代／格式限制的模式，例如：

```python
FORMAT_FOLLOW_UP = re.compile(
    r"(?:把|將|幫我)?(?:這篇|上面|剛剛|那段|這些|清單)?"
    r"(?:整理成|改成|縮成|寫成).{0,16}(?:貼文|話術|清單|員工群組|字內)"
)
```

在 `config/question_bank.json` 對應 locator 補入精準 aliases，不加入答案內容中不存在的知識。執行：

```bash
python3 scripts/build_knowledge_index.py \
  --reviewed-at 2026-09-05 \
  knowledge/designer_coaching_process.jsonl
```

- [ ] **Step 4: 重跑檢索、問法索引與 holdout 測試**

Run: `python3 -m unittest tests.test_coaching_rag tests.test_followup_chain tests.test_question_bank tests.test_holdout -v`

Expected: PASS，holdout 無退步。

- [ ] **Step 5: 提交**

```bash
git add app/service.py config/question_bank.json knowledge/designer_coaching_process.jsonl tests/test_coaching_rag.py tests/test_followup_chain.py tests/test_question_bank.py
git commit -m "fix: improve coaching retrieval and follow ups"
```

### Task 3: 模型失敗時交付最小可用答案

**Files:**
- Modify: `app/answer.py`
- Modify: `app/response_facts.py`
- Modify: `app/service.py`
- Modify: `app/quality.py`
- Test: `tests/test_service.py`
- Test: `tests/test_quality.py`

**Interfaces:**
- Produces: `response_facts.failure_reply(question: str, history=None, hits=None) -> str`
- Produces: `response_facts.source_fallback(hits: list) -> str`
- Consumes: `SearchHit.text`、`SearchHit.citation()` 與既有 model status。

- [ ] **Step 1: 寫 timeout、缺引用與無來源回退測試**

```python
def test_model_timeout_returns_cited_minimum_answer(self):
    self.service.answerer = TimeoutAnswerer()
    result = self.service.chat("完全沒記資料，第一步做什麼？")
    self.assertNotIn("重送一次", result["answer"])
    self.assertRegex(result["answer"], r"\[1\]")
    self.assertEqual(len(result["citations"]), 1)

def test_failure_without_hits_asks_one_specific_question(self):
    text = response_facts.failure_reply("生意不好怎麼辦？", hits=[])
    self.assertNotIn("重送一次", text)
    self.assertEqual(text.count("？"), 1)
```

- [ ] **Step 2: 執行測試確認目前回覆仍要求重送且無 citation**

Run: `python3 -m unittest tests.test_service tests.test_quality -v`

Expected: 新測試 FAIL，實際值包含「請重送一次」。

- [ ] **Step 3: 實作來源回退與引用裁切**

`source_fallback()` 從第一個 grounded hit 選取第一個非標題、非純說明且最長 120 字的段落或條列，輸出：

```text
這題先從一個不會出錯的動作開始
〔來源中的可執行句〕 [1]
```

`CustomerService.chat()` 與 `chat_stream()` 在非 LLM 且 `model_status` 為失敗狀態時傳入 `grounded_hits`。`_citations()` 只有在正文含合法 `[n]` 時，才為失敗回退保留被引用來源。無 hits 時回覆「先記下曝光、私訊、預約、到店與客單」並只問目前最容易取得哪一項，不要求重送。

- [ ] **Step 4: 新增通用拒答品質檢查並確認全綠**

有 grounded hits 的產品範圍回答若只含「資料不足／無法回答／重送一次」即列入 `quality.problems()`；固定 policy 回覆不送進此檢查。

Run: `python3 -m unittest tests.test_service tests.test_quality tests.test_reply_batteries -v`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add app/answer.py app/response_facts.py app/service.py app/quality.py tests/test_service.py tests/test_quality.py
git commit -m "fix: return useful answers when generation fails"
```

### Task 4: 40 題回歸集、文件與全套驗證

**Files:**
- Create: `tests/fixtures/response_reliability_40.json`
- Create: `tests/test_response_reliability.py`
- Modify: `brain.md`
- Modify: `docs/assessment-remediation.md`

**Interfaces:**
- Produces: 可離線執行的 40 題路由／安全／來源回歸集。
- Consumes: 真實 `PolicyEngine`、`Retriever`、`CustomerService._route()`；不呼叫付費模型。

- [ ] **Step 1: 建立 40 題 fixture 與獨立改寫案例**

Fixture 每題包含 `id`、`scenario`、`question`、`expected_route`、`accepted_locators`；危急題另含 `required_phrases`。對模型才能評分的成品質量標為 `requires_live_model: true`，離線測試只驗證不被錯誤 policy/low-confidence 擋下。

- [ ] **Step 2: 執行新回歸，確認至少現有低召回／安全案例會失敗**

Run: `python3 -m unittest tests.test_response_reliability -v`

Expected: 在 Task 1–3 尚未完成的基線 commit 上會失敗；在目前實作分支應通過。以 `git show` 或先前 RED 輸出保留失敗證據，不改測試迎合結果。

- [ ] **Step 3: 更新行為文件**

在 `brain.md` 記錄危急分流優先序、有來源最小回退與改寫承接；在 `docs/assessment-remediation.md` 新增 2026-09-14 的 40 題問題、修復對照及「揭露回歸非盲測」限制。

- [ ] **Step 4: 執行全部驗證**

```bash
python3 -m unittest discover -s tests
python3 scripts/evaluate_holdout.py
python3 scripts/verify_full_knowledge.py
node --check static/chat.js
git diff --check
```

Expected: unittest 0 failures、holdout 不退步、知識索引驗證成功、JavaScript 與 diff 檢查通過。

- [ ] **Step 5: 提交**

```bash
git add tests/fixtures/response_reliability_40.json tests/test_response_reliability.py brain.md docs/assessment-remediation.md
git commit -m "test: add response reliability regression suite"
```
