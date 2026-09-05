"""對話同步的時序：舊 ACK 不能清掉新修改，刪除進行中不能被合併回來。

外部稽核用受控的 fetch 時序重現過兩個問題：
  1. A 版本上傳中 → 使用者改成 B → A 的成功回覆把共用旗標清成 false → B 沒送出去。
  2. 刪除請求還沒回來時，同步讀到伺服器的舊副本，就把已刪對話合併回來。
兩個都不是「請求失敗」，是「請求成功但時序不對」，所以檢查狀態碼救不了。
這份測試抽出 `static/chat.js` 真正的函式，用可控制的 fetch 跑一次。
"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CHAT_JS = ROOT / "static" / "chat.js"


def _extract(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    # `async function` 的 async 在前面，要一起帶走。
    prefix = source.rfind("async ", max(0, start - 6), start)
    if prefix != -1:
        start = prefix
    depth = 0
    index = source.index("{", start)
    for index in range(index, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                break
    return source[start:index + 1]


HARNESS = """
const state = { conversations: [], activeId: "A", user: { id: 1 } };
const dirty = new Set();
let syncedOnce = true;
let syncTimer = null;
let pendingDeletes = new Set();
const STORAGE_PREFIX = "test";
const store = {};
const crypto = { randomUUID: () => "conflict-copy" };
Object.defineProperty(globalThis, "crypto", { value: crypto, configurable: true });
function render() {}
function el() { return null; }
function storageKey() { return "cache"; }
function newConversation() {}

const localStorage = {
  getItem: (k) => (k in store ? store[k] : null),
  setItem: (k, v) => { store[k] = String(v); },
};
function activeConversation() {
  return state.conversations.find((item) => item.id === state.activeId);
}
let respond = null;               // 由每個情境決定 fetch 什麼時候回、回什麼
const fetch = (...args) => respond(...args);
__FUNCTIONS__

async function scenarioStaleAck() {
  // A 上傳中；上傳期間使用者改了 B。A 的成功回覆不可以把 B 的 dirty 清掉。
  state.conversations = [
    { id: "A", rev: 1, messages: [{ role: "user", content: "a" }] },
    { id: "B", rev: 1, messages: [{ role: "user", content: "b" }] },
  ];
  dirty.add("A");
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  respond = async () => { await gate; return { ok: true, json: async () => ({ acks: [{ id: "A", rev: 1, status: "accepted" }] }) }; };
  const inflight = pushConversations([state.conversations[0]]);
  // 上傳還沒回來的時候改 B。
  state.activeId = "B";
  scheduleSync();
  release();
  await inflight;
  return { dirtyAfterAck: [...dirty].sort() };
}

async function scenarioStaleAckSameConversation() {
  // 更刁的一種：上傳中的是 A 的第 1 版，使用者又改了 A（第 2 版）。
  // 第 1 版的 ACK 不能把 A 從 dirty 拿掉，否則第 2 版永遠不會上傳。
  state.conversations = [{ id: "A", rev: 1, messages: [{ role: "user", content: "a" }] }];
  state.activeId = "A";
  dirty.clear();
  dirty.add("A");
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  respond = async () => { await gate; return { ok: true, json: async () => ({ acks: [{ id: "A", rev: 1, status: "accepted" }] }) }; };
  const inflight = pushConversations([{ id: "A", rev: 1, messages: [{ role: "user", content: "a" }] }]);
  scheduleSync();   // rev 變成 2
  release();
  await inflight;
  return { dirtyAfterAck: [...dirty], rev: state.conversations[0].rev };
}

async function scenarioFallbackConflictId() {
  state.conversations = [{ id: "A", title: "local", rev: 2, expected_rev: 1,
    messages: [{ content: "fallback local edit" }] }];
  state.activeId = "A";
  dirty.clear(); dirty.add("A");
  const uuid = crypto.randomUUID;
  delete crypto.randomUUID;
  let error = null;
  try { preserveConflict(state.conversations[0]); } catch (caught) { error = caught.message; }
  crypto.randomUUID = uuid;
  const item = state.conversations[0];
  return { error, copied: item.id !== "A", recoverable: dirty.has(item.id), content: item.messages[0].content };
}

async function scenarioDelayedPullAfterAck() {
  state.conversations = [{ id: "A", title: "new", rev: 2, expected_rev: 1,
    messages: [{ content: "acknowledged new content" }] }];
  dirty.clear(); dirty.add("A");
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  respond = async () => {
    await gate;
    return { ok: true, json: async () => ({ conversations: [{ id: "A", rev: 1,
      messages: [{ content: "older GET snapshot" }] }] }) };
  };
  const delayed = pullConversations();
  respond = async () => ({ ok: true, json: async () => ({ acks: [{ id: "A", rev: 2, status: "accepted" }] }) });
  await pushConversations(state.conversations);
  release();
  mergeConversations((await delayed).conversations);
  return { rev: state.conversations[0].rev, content: state.conversations[0].messages[0].content, dirty: [...dirty] };
}

async function scenarioPartialAck() {
  state.conversations = ["A", "B"].map((id) => ({ id, rev: 2, expected_rev: 1, messages: [{ content: id }] }));
  dirty.clear(); dirty.add("A"); dirty.add("B");
  respond = async () => ({ ok: true, json: async () => ({ acks: [
    { id: "A", rev: 2, status: "accepted" }, { id: "B", rev: 1, status: "accepted" }
  ] }) });
  await pushConversations(state.conversations);
  return [...dirty];
}

async function scenarioDeletedLocalEdit() {
  state.conversations = [{ id: "A", title: "local", rev: 2, expected_rev: 1, messages: [{ content: "offline edit" }] }];
  dirty.clear(); dirty.add("A");
  mergeConversations([], [{ id: "A", rev: 2 }]);
  return { id: state.conversations[0].id, content: state.conversations[0].messages[0].content, dirty: [...dirty] };
}

async function scenarioConflict() {
  state.conversations = [{ id: "A", title: "local", rev: 2, expected_rev: 1,
    messages: [{ content: "local content" }] }];
  state.activeId = "A";
  dirty.clear(); dirty.add("A");
  mergeConversations([{ id: "A", title: "remote", rev: 3, messages: [{ content: "remote content" }] }]);
  const before = JSON.stringify(state.conversations);
  mergeConversations([{ id: "A", title: "remote", rev: 3, messages: [{ content: "remote content" }] }]);
  return { items: state.conversations, dirty: [...dirty], stable: before === JSON.stringify(state.conversations) };
}

async function scenarioReload() {
  state.conversations = [{ id: "A", rev: 4, expected_rev: 2, messages:
    Array.from({ length: 25 }, (_, i) => ({ content: "message" + i })) }];
  state.activeId = "A";
  dirty.clear();
  persist();
  state.conversations = []; dirty.clear();
  load();
  return { count: state.conversations[0].messages.length, dirty: [...dirty], rev: state.conversations[0].rev };
}

async function scenarioRejectedAck() {
  state.conversations = [{ id: "A", rev: 3, messages: [{ content: "unsaved" }] }];
  dirty.clear(); dirty.add("A");
  respond = async () => ({ ok: true, json: async () => ({ saved: 0, acks: [] }) });
  await pushConversations(state.conversations);
  return [...dirty];
}

async function scenarioDeleteInFlight() {
  // 刪除請求還沒回來時，墓碑就要已經在了。
  pendingDeletes = new Set();
  let seenDuringFlight = null;
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  respond = async () => {
    seenDuringFlight = [...pendingDeletes];   // 請求在路上時同步會看到什麼
    await gate;
    return { ok: true, json: async () => ({ ack: { id: "A", status: "deleted", rev: 2 } }) };
  };
  const inflight = deleteConversationOnServer("A");
  release();
  await inflight;
  return { seenDuringFlight, afterAck: [...pendingDeletes] };
}

async function scenarioDeleteFailureSurvives() {
  pendingDeletes = new Set();
  respond = async () => ({ ok: false, status: 500 });
  await deleteConversationOnServer("A");
  const stored = JSON.parse(localStorage.getItem("test-deleted-1") || "[]");
  return { inMemory: [...pendingDeletes], persisted: stored };
}

(async () => {
  const out = {
    staleAck: await scenarioStaleAck(),
    staleAckSame: await scenarioStaleAckSameConversation(),
    rejectedAck: await scenarioRejectedAck(),
    fallback: await scenarioFallbackConflictId(),
    delayedPull: await scenarioDelayedPullAfterAck(),
    partial: await scenarioPartialAck(),
    deletedLocal: await scenarioDeletedLocalEdit(),
    conflict: await scenarioConflict(),
    reload: await scenarioReload(),
    deleteInFlight: await scenarioDeleteInFlight(),
    deleteFailure: await scenarioDeleteFailureSurvives(),
  };
  process.stdout.write(JSON.stringify(out));
})();
"""

NEEDED = (
    "pushConversations", "dirtyConversations", "scheduleSync", "makeId", "pullConversations",
    "preserveConflict", "mergeConversations", "persist", "load", "persistenceSnapshot",
    "deletesKey", "loadPendingDeletes", "savePendingDeletes",
    "deleteConversationOnServer",
)


@unittest.skipUnless(shutil.which("node"), "需要 node 才能執行 chat.js 的函式")
class SyncTimingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = CHAT_JS.read_text(encoding="utf-8")
        script = HARNESS.replace(
            "__FUNCTIONS__", "\n".join(_extract(source, name) for name in NEEDED)
        )
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "sync.js"
            path.write_text(script, encoding="utf-8")
            result = subprocess.run(
                ["node", str(path)], capture_output=True, text=True, timeout=60,
            )
        if result.returncode != 0:
            raise AssertionError(result.stderr[:2000])
        cls.out = json.loads(result.stdout)

    def test_conflict_id_fallback_keeps_local_content_recoverable(self):
        self.assertEqual(self.out["fallback"], {"error": None, "copied": True, "recoverable": True, "content": "fallback local edit"})

    def test_delayed_pull_cannot_replace_a_newer_acknowledged_revision(self):
        self.assertEqual(self.out["delayedPull"], {"rev": 2, "content": "acknowledged new content", "dirty": []})

    def test_partial_or_old_ack_does_not_clear_other_items(self):
        self.assertEqual(self.out["partial"], ["B"])

    def test_remote_deletion_keeps_unsaved_edit_as_conflict_copy(self):
        self.assertEqual(self.out["deletedLocal"], {"id": "conflict-copy", "content": "offline edit", "dirty": ["conflict-copy"]})

    def test_conflict_preserves_both_device_contents_and_is_visible(self):
        result = self.out["conflict"]
        self.assertEqual({x["messages"][0]["content"] for x in result["items"]}, {"local content", "remote content"})
        self.assertIn("conflict-copy", result["dirty"])
        self.assertTrue(any("同步衝突" in x["title"] for x in result["items"]))
        self.assertTrue(result["stable"], "pull must not create new revisions")

    def test_reload_keeps_full_unacknowledged_history_and_dirty_state(self):
        self.assertEqual(self.out["reload"], {"count": 25, "dirty": ["A"], "rev": 5})

    def test_http_success_without_item_ack_keeps_dirty(self):
        self.assertEqual(self.out["rejectedAck"], ["A"])

    def test_an_ack_does_not_clear_another_conversations_changes(self):
        """A 的成功回覆只能確認 A。共用一個布林旗標時 B 會被一起清掉。"""
        self.assertEqual(self.out["staleAck"]["dirtyAfterAck"], ["B"])

    def test_an_ack_only_confirms_the_version_it_uploaded(self):
        """上傳第 1 版的途中改成第 2 版，ACK 不能把它當成已存檔。"""
        self.assertEqual(self.out["staleAckSame"]["rev"], 2)
        self.assertEqual(self.out["staleAckSame"]["dirtyAfterAck"], ["A"])

    def test_the_tombstone_exists_while_the_delete_is_still_in_flight(self):
        """請求還在路上時同步就要看得到墓碑，否則會把已刪對話合併回來。"""
        self.assertEqual(self.out["deleteInFlight"]["seenDuringFlight"], ["A"])
        self.assertEqual(self.out["deleteInFlight"]["afterAck"], [])

    def test_a_failed_delete_is_written_to_storage(self):
        """只放記憶體的話，關掉分頁就忘了，下次登入照樣復活。"""
        self.assertEqual(self.out["deleteFailure"]["inMemory"], ["A"])
        self.assertEqual(self.out["deleteFailure"]["persisted"], ["A"])


if __name__ == "__main__":
    unittest.main()
