(() => {
  "use strict";

  const STORAGE_PREFIX = "zhang-rag-conversations-v1";
  // 開場建議：題庫最多收 100 題，每次空白對話隨機顯示 5 題。
  const WELCOME_PROMPT_POOL = 100;
  const WELCOME_PROMPT_COUNT = 5;
  // 客服模式一次最多幾顆泡泡；超過的中間會被併起來（併起來會變長訊息，
  // 所以長度控制主要靠 tuning 的 12 字規則，這裡只是安全網）。
  const SERVICE_MAX_BUBBLES = 3;
  // 一則最多幾行；模型忘了空行時前端自己重排，不要讓 8 行擠成一坨。
  const SERVICE_MAX_LINES = 2;
  // 一行最多幾個字。只數行數是擋不住的：模型常常回一整行 120 字、一個換行
  // 都沒有，行數檢查看到「1 行」就放行，畫面上就是一大坨。
  const SERVICE_MAX_CHARS = 12;
  // 客服模式的開場要像設計師自己會丟過來的一句話（他描述狀況、我接住），
  // 不是一排問句——問句是專家模式的入口。每一句都撈得到知識
  // （tests/test_welcome_prompts.py 逐句驗證）。
  const SERVICE_WELCOME_PROMPTS = [
    "私訊很多但沒人來",
    "客人問完價格就已讀",
    "廣告花了 5000 只來 1 個",
    "客人說太貴",
    "這個月業績掉了三成",
    "客人約了沒來",
    "客人說再想想",
    "我很久沒發作品了",
    "客人染壞了要我退錢",
    "我每天都做到很晚",
  ];
  const state = {
    conversations: [], activeId: null, controller: null,
    user: null,
    tone: "expert",
    profile: "designer_coach",
    assistantName: "AI 輔導教練",
    welcomePrompts: [
      "設計師私訊很多但預約很少，先查什麼？",
      "幫我安排一次 1 對 1 輔導流程",
      "如何健檢設計師的私訊回覆？",
    ],
  };
  const el = (id) => document.getElementById(id);
  const messages = el("messages");
  const prompt = el("prompt");

  function storageKey() {
    return `${STORAGE_PREFIX}-${state.profile}-${state.user?.id || "anonymous"}`;
  }

  // 語氣設定：expert＝完整條列講深講透；service＝像真人聊天一句一句回。
  function toneKey() {
    return `${STORAGE_PREFIX}-tone-${state.user?.id || "anonymous"}`;
  }

  function setTone(tone, save = true) {
    state.tone = tone === "service" ? "service" : "expert";
    document.querySelectorAll("#tone-toggle .tone-option").forEach((button) => {
      const active = button.dataset.tone === state.tone;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    // 右上角常駐顯示目前的回覆模式。
    el("tone-indicator").textContent = state.tone === "service" ? "客服模式" : "專家模式";
    if (save) {
      try { localStorage.setItem(toneKey(), state.tone); } catch (_) { /* 存不進去就用預設 */ }
      if (syncedOnce) savePref("tone", state.tone);
    }
  }

  function loadTone() {
    try { setTone(localStorage.getItem(toneKey()), false); } catch (_) { setTone("expert", false); }
  }

  function showLogin(message = "") {
    state.user = null;
    el("account-menu").hidden = true;
    el("app-shell").hidden = true;
    el("login-gate").hidden = false;
    el("login-status").textContent = message;
    el("login-password").value = "";
    requestAnimationFrame(() => el("login-username").focus());
    window.lucide?.createIcons();
  }

  function showApp(user) {
    state.user = user;
    el("login-gate").hidden = true;
    el("app-shell").hidden = false;
    el("user-name").textContent = user.username;
    el("profile-avatar").textContent = Array.from(user.username)[0]?.toUpperCase() || "U";
    const adminLink = el("admin-link");
    if (adminLink) adminLink.hidden = user.role !== "admin";
    window.lucide?.createIcons();
  }

  function makeId() {
    return globalThis.crypto?.randomUUID?.() || `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function newConversation() {
    // 已經有一個還沒開始的空白對話就直接切過去，不要再疊一個。
    const empty = state.conversations.find((conversation) => !(conversation.messages || []).length);
    if (empty) {
      // 語氣綁在對話上，空白的那段直接改成現在的語氣就好。
      empty.tone = state.tone;
      state.activeId = empty.id;
    } else {
      const conversation = {
        id: makeId(), title: "新對話", tone: state.tone,
        createdAt: new Date().toISOString(), messages: [],
      };
      state.conversations.unshift(conversation);
      state.activeId = conversation.id;
      persist();
    }
    render();
    closeSidebar();
    prompt.focus();
  }

  function load() {
    try {
      state.conversations = JSON.parse(localStorage.getItem(storageKey()) || "[]");
    } catch (_) {
      state.conversations = [];
    }
    dirty.clear();
    if (Array.isArray(state.conversations)) {
      state.conversations.forEach((item) => { if (item._dirty) dirty.add(item.id); });
      // A loading placeholder saved mid-request would spin forever after reload.
      state.conversations.forEach((conversation) => {
        if (Array.isArray(conversation?.messages)) {
          conversation.messages = conversation.messages.filter((message) => !message?.loading);
          // 逐句顯示只在收到回覆的當下跑一次，重新整理後直接全部顯示。
          conversation.messages.forEach((message) => { delete message.pendingReveal; });
        }
      });
    }
    if (!Array.isArray(state.conversations) || !state.conversations.length) newConversation();
    else state.activeId = state.conversations[0].id;
  }

  function persistenceSnapshot() {
    return state.conversations.map((conversation) => ({
      ...conversation,
      messages: (conversation.messages || []).filter((message) => !message.loading),
    }));
  }

  // ---- 對話紀錄同步到伺服器（換裝置也看得到；localStorage 只當離線快取）----
  let syncTimer = null;
  let syncedOnce = false;
  // 哪幾段對話有還沒送出去的修改。**要記到「哪一段」而不是一個布林值**：
  // 共用一個旗標時，A 上傳中、使用者改了 B，A 的成功回覆會把 B 的旗標一起清掉，
  // B 就再也不會被送出去。每段對話另外帶一個 rev，ACK 只能確認它上傳的那一版。
  const dirty = new Set();

  async function pullConversations() {
    try {
      const response = await fetch("/api/conversations", { headers: { Accept: "application/json" } });
      if (!response.ok) return null;
      return await response.json();
    } catch (_) {
      return null;  // 連不上就先用本機那份，等下次再同步
    }
  }

  function preserveConflict(item) {
    const originalId = item.id;
    const conflictId = makeId();
    dirty.delete(originalId);
    item.id = conflictId;
    item.title = `${item.title || "對話"}（同步衝突・本機保留）`;
    item.rev = 1;
    item.expected_rev = 0;
    item._dirty = true;
    dirty.add(item.id);
    if (state.activeId === originalId) state.activeId = item.id;
    render();
  }

  async function pushConversations(conversations) {
    if (!conversations.length) return;
    const sent = conversations.map((item) => ({ ...item,
      expected_rev: item.expected_rev ?? 0,
      messages: (item.messages || []).filter((message) => !message.loading),
    }));
    try {
      const response = await fetch("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversations: sent }),
      });
      if (!response.ok) return;
      const body = await response.json();
      for (const ack of (Array.isArray(body.acks) ? body.acks : [])) {
        const uploaded = sent.find((item) => item.id === ack.id && (item.rev || 0) === ack.rev);
        const current = state.conversations.find((item) => item.id === ack.id);
        if (!uploaded || !current) continue;
        if (ack.status === "accepted") {
          current.expected_rev = Math.max(current.expected_rev || 0, ack.rev);
          if ((current.rev || 0) === ack.rev) {
            dirty.delete(current.id);
            delete current._dirty;
          }
        } else if (ack.status === "conflict" || ack.status === "deleted") {
          // 已確認的舊請求不可以把後來的新版本誤判成衝突。
          if ((current.expected_rev || 0) < ack.rev || ack.status === "deleted") preserveConflict(current);
        }
      }
      persist(false);
    } catch (_) { /* 未收到逐筆確認，保留完整本機修改等待重試。 */ }
  }

  function dirtyConversations() {
    return state.conversations.filter(
      (item) => dirty.has(item.id) && !pendingDeletes.has(item.id) && (item.messages || []).length
    );
  }

  function scheduleSync(conversation = activeConversation()) {
    if (conversation) {
      conversation.expected_rev ??= 0;
      conversation.rev = (conversation.rev || 0) + 1;
      conversation._dirty = true;
      dirty.add(conversation.id);
    }
    clearTimeout(syncTimer);
    if (!syncedOnce) return;
    syncTimer = setTimeout(() => {
      const pending = dirtyConversations();
      if (pending.length) pushConversations(pending);
    }, 1200);
  }

  // 已經按下刪除、但還不確定伺服器那邊刪掉了的對話（tombstone）。
  // **要在送出請求之前就記，而且要存進 localStorage**：
  //  - 只在失敗後才記的話，請求還在路上時剛好同步，就會把它從伺服器合併回來，
  //    使用者看到「明明刪掉的對話自己活過來」，而且刪第二次還是刪不掉。
  //  - 只放在記憶體的話，關掉分頁就忘了，下次登入照樣復活。
  let pendingDeletes = new Set();

  function deletesKey() {
    return `${STORAGE_PREFIX}-deleted-${state.user?.id || "anonymous"}`;
  }

  function loadPendingDeletes() {
    try {
      const raw = JSON.parse(localStorage.getItem(deletesKey()) || "[]");
      pendingDeletes = new Set(Array.isArray(raw) ? raw.map(String) : []);
    } catch (_) { pendingDeletes = new Set(); }
  }

  function savePendingDeletes() {
    try {
      localStorage.setItem(deletesKey(), JSON.stringify([...pendingDeletes]));
    } catch (_) { /* 無痕模式：這一輪還是擋得住，只是關掉分頁就忘了 */ }
  }

  async function deleteConversationOnServer(id) {
    // 先立墓碑再送。中間這段時間的任何一次同步都不會把它合併回來。
    pendingDeletes.add(id);
    savePendingDeletes();
    try {
      const response = await fetch("/api/conversations/delete", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ id }),
      });
      const body = response.ok ? await response.json() : null;
      if (body?.ack?.id === id && body.ack.status === "deleted") {
        pendingDeletes.delete(id);
        savePendingDeletes();
        return true;
      }
    } catch (_) { /* 墓碑留著，下次同步時再刪一次 */ }
    return false;
  }

  async function savePref(key, value) {
    try {
      await fetch("/api/prefs", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prefs: { [key]: value } }),
      });
    } catch (_) { /* 存不上去就維持本機設定 */ }
  }

  function persist(conversation = activeConversation()) {
    if (conversation !== false) scheduleSync(conversation);
    try {
      // 未確認內容必須完整保存；不可將截斷的離線快取當作完整歷史上傳。
      localStorage.setItem(storageKey(), JSON.stringify(persistenceSnapshot()));
      return true;
    } catch (_) {
      const title = el("conversation-title");
      if (title) title.textContent = "本機儲存空間不足，請保持此頁開啟直到同步完成";
      return false;
    }
  }

  function activeConversation() {
    return state.conversations.find((item) => item.id === state.activeId);
  }

  function deleteConversation(id) {
    deleteConversationOnServer(id);
    dirty.delete(id);
    state.conversations = state.conversations.filter((conversation) => conversation.id !== id);
    if (!state.conversations.length) {
      newConversation();
      return;
    }
    if (state.activeId === id) state.activeId = state.conversations[0].id;
    persist(false);
    render();
  }

  // 對話清單顯示最後活動的日期時間（沒有就用建立時間）。
  function formatConversationTime(conversation) {
    const date = new Date(conversation.updatedAt || conversation.createdAt || Date.now());
    if (Number.isNaN(date.getTime())) return "";
    const hhmm = `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
    return `${date.getMonth() + 1}/${date.getDate()} ${hhmm}`;
  }

  function renderSidebar() {
    const list = el("conversation-list");
    const query = el("conversation-search-input").value.trim().toLowerCase();
    list.replaceChildren();
    state.conversations.filter((conversation) => conversation.title.toLowerCase().includes(query)).forEach((conversation, index) => {
      const item = document.createElement("div");
      item.className = `conversation-item${conversation.id === state.activeId ? " active" : ""}`;
      item.setAttribute("role", "button");
      item.tabIndex = 0;
      // 編號從最新那一段開始數，跟清單順序一致（最上面是 1）。
      const order = document.createElement("span");
      order.className = "conversation-order";
      order.textContent = index + 1;
      order.setAttribute("aria-hidden", "true");
      const label = document.createElement("span");
      label.className = "conversation-copy";
      const name = document.createElement("span");
      name.className = "conversation-name";
      name.textContent = conversation.title;
      const time = document.createElement("span");
      time.className = "conversation-time";
      time.textContent = formatConversationTime(conversation);
      label.append(name, time);
      const remove = document.createElement("button");
      remove.className = "conversation-delete";
      remove.type = "button";
      remove.title = "刪除對話";
      remove.setAttribute("aria-label", `刪除對話：${conversation.title}`);
      remove.innerHTML = '<i data-lucide="trash-2"></i>';
      // 不跳瀏覽器原生視窗：第一下變成「確定刪除」，3 秒內再按一下才真的刪。
      remove.addEventListener("click", (event) => {
        event.stopPropagation();
        if (remove.classList.contains("confirming")) {
          deleteConversation(conversation.id);
          return;
        }
        remove.classList.add("confirming");
        remove.innerHTML = '<span class="confirm-delete">確定刪除</span>';
        setTimeout(() => {
          if (!remove.isConnected || !remove.classList.contains("confirming")) return;
          remove.classList.remove("confirming");
          remove.innerHTML = '<i data-lucide="trash-2"></i>';
          window.lucide?.createIcons();
        }, 3000);
      });
      const select = () => {
        state.activeId = conversation.id;
        render();
        closeSidebar();
      };
      item.addEventListener("click", select);
      item.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); select(); }
      });
      item.append(order, label, remove);
      list.append(item);
    });
  }

  function pickRandom(items, count) {
    const pool = [...(items || [])];
    for (let index = pool.length - 1; index > 0; index -= 1) {
      const swap = Math.floor(Math.random() * (index + 1));
      [pool[index], pool[swap]] = [pool[swap], pool[index]];
    }
    return pool.slice(0, count);
  }

  function welcomeView() {
    const wrapper = document.createElement("div");
    wrapper.className = "welcome";
    wrapper.innerHTML = `
      <h2>${state.tone === "service" ? "最近卡在哪裡？" : "我們該從哪裡開始？"}</h2>
      <div class="prompt-list"></div>`;
    const list = wrapper.querySelector(".prompt-list");
    // 每次進到空白對話都從整個題庫隨機換一組；客服模式換成狀況句。
    const pool = state.tone === "service" ? SERVICE_WELCOME_PROMPTS : state.welcomePrompts;
    pickRandom(pool, WELCOME_PROMPT_COUNT).forEach((label) => {
      const button = document.createElement("button");
      button.className = "prompt-suggestion";
      button.type = "button";
      button.innerHTML = '<i data-lucide="sparkles"></i>';
      const text = document.createElement("span");
      text.textContent = label;
      button.append(text);
      list.append(button);
    });
    wrapper.querySelectorAll(".prompt-suggestion").forEach((button) => button.addEventListener("click", () => {
      prompt.value = button.querySelector("span").textContent;
      updateComposer();
      prompt.focus();
    }));
    return wrapper;
  }

  function messageView(item, isLast = false) {
    const row = document.createElement("article");
    row.className = `message-row ${item.role}`;
    const avatar = document.createElement("div");
    avatar.className = "message-avatar";
    avatar.innerHTML = item.role === "user" ? '<i data-lucide="user"></i>' : '<i data-lucide="sparkles"></i>';
    const content = document.createElement("div");
    content.className = "message-content";
    const role = document.createElement("div");
    role.className = "message-role";
    role.textContent = item.role === "user" ? "你" : state.assistantName;
    const text = document.createElement("div");
    text.className = "message-text";
    if (item.loading) {
      text.innerHTML = '<div class="typing" aria-label="正在查詢"><span></span><span></span><span></span></div>'
        + '<p class="wait-hint" hidden></p>';
    } else if (item.role === "assistant") {
      text.classList.add("rich");
      // 降級成知識原文時不逐句拆泡泡（會變成幾十則），維持一般排版＋降級標籤。
      if (item.tone === "service" && item.modelStatus === "used") {
        // 客服模式：每一行都是一則獨立訊息，畫成一顆一顆的聊天泡泡。
        text.classList.add("bubbles");
        text.innerHTML = renderServiceBubbles(item.content);
      } else {
        text.innerHTML = renderAssistantMarkup(item.content, item.citations?.length || 0);
      }
      text.querySelectorAll(".cite-ref").forEach((ref) => {
        ref.addEventListener("click", () => {
          const index = Number(ref.dataset.cite) - 1;
          if (item.citations?.length) openSources(item.citations, Math.max(0, Math.min(index, item.citations.length - 1)));
        });
      });
    } else {
      text.textContent = item.content;
    }
    content.append(role, text);

    // 客服模式是「真人在傳訊息」，泡泡下面不該長出系統徽章。
    // 正常回覆時整個藏起來，只有降級或轉真人時才顯示（那時候要講清楚）。
    const chatty = item.tone === "service" && item.modelStatus === "used";
    if (item.status && !(chatty && item.status === "answered")) {
      const status = document.createElement("div");
      const answered = item.status === "answered";
      // 模型沒回應時要講清楚，不然看起來像 AI 亂答。
      const degraded = answered && item.modelStatus
        && !["used", "not_configured", "boundary"].includes(item.modelStatus);
      status.className = `message-status ${degraded ? "degraded" : item.status}`;
      // 內部狀態碼（missing_citations 之類）不外露給使用者，只放進 title 供除錯。
      // 邊界題（離題／不當請求／問身分）是刻意的固定回應，不是知識庫回答。
      const boundary = item.modelStatus === "boundary";
      // 徽章只講狀態，AI 想說的話留在泡泡裡（不要讓系統替 AI 講話）。
      // 「查不到資料」不是「需要人來判斷」——這裡沒有人可以轉，
      // 對一句「謝謝」回這個標籤是這個產品最傷的一幕。
      const softFallback = !answered && ["no_results", "low_confidence"].includes(item.reason);
      const label = answered
        ? (degraded ? "這則沒有成功生成" : boundary ? "這題不在輔導範圍" : "已根據知識庫回答")
        : (softFallback ? "這題我先不亂答" : "這題要你自己決定");
      if (boundary) status.classList.add("boundary");
      if (softFallback) status.classList.add("soft");
      const icon = answered
        ? (degraded ? "triangle-alert" : boundary ? "info" : "badge-check")
        : (softFallback ? "message-circle-question" : "user-round-check");
      status.innerHTML = `<i data-lucide="${icon}"></i><span>${label}</span>`;
      if (degraded && item.modelStatus) status.title = `model_status: ${item.modelStatus}`;
      content.append(status);
    }

    // 連線失敗時給一個重送按鈕，並把原本的問題放回輸入框。
    if (item.retryPrompt) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "followup-button retry-button";
      retry.innerHTML = '<i data-lucide="rotate-ccw"></i>';
      const label = document.createElement("span");
      label.textContent = "重送這則";
      retry.append(label);
      retry.addEventListener("click", () => {
        prompt.value = item.retryPrompt;
        updateComposer();
        sendMessage();
      });
      content.append(retry);
    }

    // 每則回答都能複製與評分（👍👎），回饋存進伺服器供之後加強知識。
    // 複製不綁 traceId：沒有評分 id 的回答（例如離線快取）照樣要能複製。
    if (item.role === "assistant" && item.status === "answered" && !item.loading) {
      const feedback = document.createElement("div");
      feedback.className = "feedback-row";
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "feedback-button copy-button";
      copy.title = "複製這則回答";
      copy.setAttribute("aria-label", "複製這則回答");
      copy.innerHTML = `<i data-lucide="copy"></i>`;
      copy.addEventListener("click", async () => {
        const ok = await copyText(plainAnswer(item.content));
        copy.classList.toggle("copied", ok);
        copy.title = ok ? "已複製" : "複製失敗，請長按選取";
        copy.setAttribute("aria-label", copy.title);
        copy.innerHTML = `<i data-lucide="${ok ? "check" : "copy"}"></i>`;
        window.lucide?.createIcons();
        window.setTimeout(() => {
          copy.classList.remove("copied");
          copy.title = "複製這則回答";
          copy.setAttribute("aria-label", copy.title);
          copy.innerHTML = `<i data-lucide="copy"></i>`;
          window.lucide?.createIcons();
        }, 1600);
      });
      feedback.append(copy);
      if (item.traceId) [["up", "thumbs-up", "這則回答有幫助"], ["down", "thumbs-down", "這則回答沒幫助"]].forEach(([rating, icon, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `feedback-button${item.feedback === rating ? " selected" : ""}`;
        button.dataset.rating = rating;
        button.title = label;
        button.setAttribute("aria-label", label);
        button.innerHTML = `<i data-lucide="${icon}"></i>`;
        button.addEventListener("click", () => sendFeedback(item, rating));
        feedback.append(button);
      });
      content.append(feedback);
    }

    if (item.citations?.length && chatty) {
      // 客服模式把來源收成一顆小鈕：想查得到，但不會把聊天畫面變成報表。
      const compact = document.createElement("div");
      compact.className = "citation-list compact";
      const button = document.createElement("button");
      button.className = "citation-button";
      button.type = "button";
      button.innerHTML = '<i data-lucide="book-open"></i>';
      const label = document.createElement("span");
      label.textContent = `來源 ${item.citations.length}`;
      button.append(label);
      button.addEventListener("click", () => openSources(item.citations, 0));
      compact.append(button);
      content.append(compact);
    } else if (item.citations?.length) {
      const citations = document.createElement("div");
      citations.className = "citation-list";
      const citationLabel = document.createElement("span");
      citationLabel.className = "citation-label";
      citationLabel.textContent = "知識來源：";
      citations.append(citationLabel);
      item.citations.slice(0, 6).forEach((citation, index) => {
        const button = document.createElement("button");
        button.className = "citation-button";
        button.type = "button";
        button.innerHTML = `<span class="citation-number">${index + 1}</span><span>${escapeHtml(citation.section_title || citation.title)}</span>`;
        button.addEventListener("click", () => openSources(item.citations, index));
        citations.append(button);
      });
      content.append(citations);
    }

    if (isLast && item.role === "assistant" && item.followups?.length && !state.controller) {
      const followups = document.createElement("div");
      followups.className = "followup-list";
      item.followups.slice(0, 3).forEach((question) => {
        const button = document.createElement("button");
        button.className = "followup-button";
        button.type = "button";
        button.innerHTML = '<i data-lucide="corner-down-right"></i>';
        const label = document.createElement("span");
        label.textContent = question;
        button.append(label);
        button.addEventListener("click", () => {
          prompt.value = question;
          updateComposer();
          sendMessage();
        });
        followups.append(button);
      });
      content.append(followups);
    }
    row.append(avatar, content);
    return row;
  }

  // 客服模式像真人打字：第一句直接出現，之後每句隨機停 1.5~2.5 秒再發，
  // 中間掛著輸入中的點點；狀態列、來源與追問等全部發完才顯示。
  function revealServiceMessage(item) {
    const row = messages.lastElementChild;
    const text = row?.querySelector(".message-text.bubbles");
    if (!text) return;
    const lines = [...text.querySelectorAll(".chat-line")];
    const extras = [...row.querySelectorAll(".message-status, .feedback-row, .citation-list, .followup-list")];
    if (lines.length < 2 && !extras.length) return;
    lines.forEach((line) => { line.hidden = true; });
    extras.forEach((extra) => { extra.hidden = true; });
    const typing = document.createElement("div");
    typing.className = "typing";
    typing.setAttribute("aria-label", "輸入中");
    typing.innerHTML = "<span></span><span></span><span></span>";
    text.append(typing);
    let index = 0;
    const step = () => {
      if (item !== activeConversation()?.messages?.[activeConversation().messages.length - 1]) return;
      if (index < lines.length) {
        lines[index].hidden = false;
        index += 1;
        messages.scrollTop = messages.scrollHeight;
        // 一則跟下一則之間至少 3 秒——真人打字沒那麼快，
        // 太快跳出來就只是一次倒完，讀的人來不及看完上一則。
        setTimeout(step, 3000 + Math.random() * 1500);
        return;
      }
      typing.remove();
      extras.forEach((extra) => { extra.hidden = false; });
      messages.scrollTop = messages.scrollHeight;
      window.lucide?.createIcons();
    };
    step();
  }

  // 就地切換語氣：留在同一段對話，只是把這一段之後的回覆換成另一種人格
  // （使用者指定）。已經送出的訊息各自記著自己的 tone，所以往上捲仍然是
  // 當時的樣子；這一段對話也記下新語氣，之後切回來看到的就是它。
  function switchTone(tone) {
    if (!tone || tone === state.tone) return;
    setTone(tone);
    const conversation = activeConversation();
    if (conversation) {
      conversation.tone = state.tone;
      persist();
    }
    render();
  }

  function applyConversationTone() {
    const conversation = activeConversation();
    // 存起來的語氣是這段對話的一部分：切回舊對話要看到當時的樣子，
    // 但不要把它寫回個人偏好（那是「下一段新對話用哪一種」）。
    if (conversation?.tone && conversation.tone !== state.tone) setTone(conversation.tone, false);
  }

  function renderMessages() {
    applyConversationTone();
    const conversation = activeConversation();
    messages.replaceChildren();
    const isEmpty = !conversation.messages.length;
    el("chat-main").classList.toggle("is-empty", isEmpty);
    if (isEmpty) messages.append(welcomeView());
    else conversation.messages.forEach((item, index) => messages.append(messageView(item, index === conversation.messages.length - 1)));
    el("conversation-title").textContent = conversation.title;
    const last = conversation.messages[conversation.messages.length - 1];
    if (last?.pendingReveal) {
      last.pendingReveal = false;
      revealServiceMessage(last);
    }
    requestAnimationFrame(() => { messages.scrollTop = messages.scrollHeight; window.lucide?.createIcons(); });
  }

  function render() {
    renderSidebar();
    renderMessages();
    window.lucide?.createIcons();
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  // Minimal Markdown for model answers. Input is HTML-escaped first, so only
  // the markup generated here reaches innerHTML.
  const BULLET_LINE = /^\s*[-*•]\s+(.*)$/;
  const ORDERED_LINE = /^\s*(\d{1,2})[.)]\s+(.*)$/;
  const HEADING_LINE = /^\s*#{1,3}\s+(.*)$/;

  // 等太久要講一句話。三顆點只證明畫面沒死，說不出「還要多久」，也說不出
  // 「它在幹嘛」——推理模型一題寫 40 秒是常態（`LLM_TIMEOUT_SECONDS` 給到 60），
  // 中間完全沒有交代，看起來就像當掉。
  // 階段是照實際流程排的：先檢索、再生成、超過 25 秒通常是題目本身難。
  const WAIT_HINTS = [
    [5000, "正在查知識庫"],
    [12000, "正在整理回答"],
    [25000, "這題比較複雜，還在寫"],
    [45000, "快好了，再等一下"],
  ];
  // 品質重打的階段：字已經吐完才發現不合格，從 0 秒就要講，不能再從
  // 「正在查知識庫」重數一輪。
  const REFINE_HINTS = [
    [0, "這則回答不夠具體，正在重寫"],
    [20000, "快好了，再等一下"],
  ];
  let waitTimer = null;
  let waitStartedAt = 0;
  let waitHints = WAIT_HINTS;

  function paintWaitHint() {
    const hint = messages.querySelector(".message-text .wait-hint");
    if (!hint) return;
    const elapsed = Date.now() - waitStartedAt;
    const stage = waitHints.filter(([after]) => elapsed >= after).pop();
    if (!stage) {
      hint.hidden = true;
      return;
    }
    // 秒數要真的在跳：只寫「正在整理回答」看久了跟卡住沒兩樣。
    hint.hidden = false;
    hint.textContent = `${stage[1]}（已等 ${Math.round(elapsed / 1000)} 秒）`;
  }

  function startWaitHint(hints = WAIT_HINTS) {
    stopWaitHint();
    waitHints = hints;
    waitStartedAt = Date.now();
    // 每秒重找一次節點：render() 隨時可能把整串訊息重畫掉。
    waitTimer = window.setInterval(paintWaitHint, 1000);
    paintWaitHint();
  }

  function stopWaitHint() {
    if (waitTimer) window.clearInterval(waitTimer);
    waitTimer = null;
  }

  function inlineMarkup(text, citationCount) {
    let html = escapeHtml(text);
    html = html.replace(/`([^`\n]+)`/g, "<code>$1</code>");
    html = html.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
    if (citationCount > 0) {
      html = html.replace(/\[(\d{1,2})\]/g, (match, number) => (
        Number(number) >= 1 && Number(number) <= citationCount
          ? `<button type="button" class="cite-ref" data-cite="${number}">${number}</button>`
          : match
      ));
    }
    return html;
  }

  // Answers come back as a one-line conclusion plus bullets; render the lists
  // as real list elements so they stay scannable instead of one wall of text.
  function renderAssistantMarkup(content, citationCount) {
    const blocks = [];
    let list = null;
    let paragraph = [];
    // 編號數到哪了。模型幾乎都會在每個編號底下再寫一段說明，那一段會把清單
    // 收掉，下一個編號就落在新的 <ol> 裡——瀏覽器預設每個 <ol> 都從 1 開始數，
    // 畫面上七個步驟全部變成「1.」。所以自己記著，新開的 <ol> 用 start 接上去。
    // 這也順便修掉「模型自己每一項都寫 1.」的情況。標題代表換一段，才歸零。
    let orderedNext = 0;

    const flushParagraph = () => {
      if (paragraph.length) blocks.push(`<p>${paragraph.join("<br>")}</p>`);
      paragraph = [];
    };
    const flushList = () => {
      if (list) {
        const start = list.tag === "ol" && list.start > 1 ? ` start="${list.start}"` : "";
        blocks.push(`<${list.tag}${start}>${list.items.join("")}</${list.tag}>`);
      }
      list = null;
    };

    for (const rawLine of String(content || "").split("\n")) {
      const line = rawLine.trimEnd();
      if (!line.trim()) {
        // 空行只收段落，清單要留著。模型常常在每個編號之間空一行，這裡收掉的話
        // 每一項都會各自變成一個清單，畫面上就全部是「1.」。
        // 真的該收的時候（下一行是段落或標題）那兩條路自己會收。
        flushParagraph();
        continue;
      }
      const heading = line.match(HEADING_LINE);
      if (heading) {
        flushParagraph();
        flushList();
        orderedNext = 0;
        blocks.push(`<p class="md-heading">${inlineMarkup(heading[1], citationCount)}</p>`);
        continue;
      }
      const bullet = line.match(BULLET_LINE);
      const ordered = bullet ? null : line.match(ORDERED_LINE);
      if (bullet || ordered) {
        flushParagraph();
        const tag = bullet ? "ul" : "ol";
        // 第一項用模型自己寫的號碼（他從 3 開始就從 3 開始），之後一律往下數。
        if (ordered) orderedNext = orderedNext > 0 ? orderedNext + 1 : Number(ordered[1]) || 1;
        if (!list || list.tag !== tag) {
          flushList();
          list = { tag, items: [], start: ordered ? orderedNext : 1 };
        }
        list.items.push(`<li>${inlineMarkup(bullet ? bullet[1] : ordered[2], citationCount)}</li>`);
        continue;
      }
      flushList();
      paragraph.push(inlineMarkup(line, citationCount));
    }
    flushParagraph();
    flushList();
    return blocks.join("");
  }

  // 一鍵複製要給的是「貼到 LINE 就能用」的純文字：拿掉引用編號與 Markdown
  // 記號，但保留條列的破折號與換行——設計師複製的多半就是話術與清單。
  function plainAnswer(content) {
    return String(content || "")
      .split("\n")
      .map((rawLine) => {
        const line = rawLine.trimEnd();
        const heading = line.match(HEADING_LINE);
        const body = heading ? heading[1] : line;
        return body
          .replace(/\s*\[\d{1,2}\]/g, "")
          .replace(/\*\*([^*\n]+)\*\*/g, "$1")
          .replace(/`([^`\n]+)`/g, "$1")
          .trimEnd();
      })
      .join("\n")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  // navigator.clipboard 只在安全內容（HTTPS／localhost）下存在，PWA 以外的
  // 情境（區網 http 測試）會整個 undefined，所以留一條 textarea 的後路。
  async function copyText(text) {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        return true;
      }
    } catch (error) {
      // 使用者拒絕權限或非安全內容：往下走後路。
    }
    const scratch = document.createElement("textarea");
    scratch.value = text;
    scratch.setAttribute("readonly", "");
    scratch.style.position = "fixed";
    scratch.style.opacity = "0";
    document.body.append(scratch);
    scratch.select();
    let ok = false;
    try {
      ok = document.execCommand("copy");
    } catch (error) {
      ok = false;
    }
    scratch.remove();
    return ok;
  }

  // 客服模式：像真人一句一句發訊息。
  // - 引用編號只給系統核對，句尾不顯示 [1]——來源照樣列在泡泡下方。
  // - 標點一律拿掉，以空白分段，像平常打字（「～」保留）。
  // - 依語意斷句：模型的每一行就是一則訊息，不做字數硬拆（硬拆會把句子切壞）。
  // 半形的「.」與「:」只有夾在數字中間時才留著（8.5%、10:30）；其餘位置是標點。
  // 不用 lookbehind 正規表示式，舊版 iOS Safari 不支援。
  function stripAsciiDots(line) {
    let out = "";
    for (let index = 0; index < line.length; index += 1) {
      const char = line[index];
      if (char === "." || char === ":") {
        const inNumber = /\d/.test(line[index - 1] || "") && /\d/.test(line[index + 1] || "");
        out += inNumber ? char : " ";
      } else {
        out += char;
      }
    }
    return out;
  }

  function cleanChatLine(line) {
    return stripAsciiDots(
      line
        .trim()
        .replace(/^(?:[-*•]|\d{1,2}[.)])\s+/, "")
        .replace(/\s*\[\d{1,2}\]/g, "")
        .replace(/[，。、；：？?,;「」『』（）()]/g, " "),
    )
      .replace(/\s+/g, " ")
      .trim();
  }

  // 一則訊息裡面可以有好幾行（像「我想要吃 / 海鮮 / 玉米 / 薯條」）：
  // **空一行才代表換一則**，單純換行只是同一則裡的下一行。
  // 把過長的一行斷成每行 12 字以內。標點已經拿掉，空白就是唯一的斷句記號，
  // 所以照空白切再把短的併回去，不會切在詞的中間；單一段超過兩倍才硬切。
  // 字數只算內容不算空白——把空白算進去的話，「這週抓 20 則」這種正常句子
  // 會被拆成兩行。
  // 兩個字以內的一段不會是獨立的句子，它是後面那段的一部分（「抓 20 則來看」
  // 被空白切成「抓」「20」「則來看」）。不先黏回去，斷行會落在數字跟量詞中間。
  function glueFragments(segments) {
    const glued = [];
    let pending = "";
    segments.forEach((segment) => {
      pending = pending ? `${pending} ${segment}` : segment;
      if (segment.length >= 3) { glued.push(pending); pending = ""; }
    });
    if (pending) {
      if (glued.length) glued[glued.length - 1] += ` ${pending}`;
      else glued.push(pending);
    }
    return glued;
  }

  function wrapLine(line, cap = SERVICE_MAX_CHARS) {
    const lines = [];
    let current = "";
    glueFragments(String(line || "").split(" ").filter(Boolean)).forEach((piece) => {
      let segment = piece;
      while (segment.length > cap * 2) {
        if (current) { lines.push(current); current = ""; }
        lines.push(segment.slice(0, cap));
        segment = segment.slice(cap);
      }
      if (!current) current = segment;
      else if (current.replace(/ /g, "").length + segment.replace(/ /g, "").length <= cap) current = `${current} ${segment}`;
      else { lines.push(current); current = segment; }
    });
    if (current) lines.push(current);
    return lines;
  }

  // 「？」不能整顆吃掉：問句剝掉問號、又沒有語助詞收尾，讀起來像冷冷的
  // 陳述句。句尾已有「嗎」「呢」這類字的照舊拿掉；沒有的把「？」換成「～」
  // （使用者決定）。app/humanize.py 的 soften_questions 是同一份實作，
  // 改一邊要改兩邊（tests/split_vectors.json 有共用向量守著）。
  function softenQuestions(text) {
    const endings = "嗎呢吧嘛麼么～唷呀啦喔哦欸！!";
    return String(text || "").replace(/[？?]+/g, (match, offset, source) => {
      const previous = offset ? source[offset - 1] : "";
      return endings.includes(previous) ? " " : "～ ";
    });
  }

  function serviceSentences(content) {
    const bubbles = softenQuestions(String(content || ""))
      .split(/\n[ \t]*\n+/)
      .map((block) => block.split("\n").map(cleanChatLine).filter(Boolean).join("\n"))
      .filter(Boolean);
    // 每次最多 3 則（硬上限）。模型超寫時把中間併成一則，不能直接丟掉——
    // 丟中間會把「範例」「話術」的正文整段吃掉，只剩開頭跟結尾，答案就不到位了。
    // 併起來時用換行接（不是空白），才不會變成一長條讀不動的句子。
    // 先把過長的一則重排成每則最多 2 行——模型常常忘了空行，整段就變成一坨。
    const reflowed = [];
    bubbles.forEach((bubble) => {
      // 先斷行再數行數，順序反了就擋不住「一行 120 字」。
      const lines = bubble.split("\n").flatMap((line) => wrapLine(line));
      for (let index = 0; index < lines.length; index += SERVICE_MAX_LINES) {
        reflowed.push(lines.slice(index, index + SERVICE_MAX_LINES).join("\n"));
      }
    });
    if (reflowed.length <= SERVICE_MAX_BUBBLES) return reflowed;
    return [
      ...reflowed.slice(0, SERVICE_MAX_BUBBLES - 2),
      reflowed.slice(SERVICE_MAX_BUBBLES - 2, -1).join("\n"),
      reflowed[reflowed.length - 1],
    ];
  }

  function renderServiceBubbles(content) {
    return serviceSentences(content)
      .map((bubble) => {
        const html = bubble.split("\n").map((line) => inlineMarkup(line, 0)).join("<br>");
        return `<p class="chat-line">${html}</p>`;
      })
      .join("");
  }

  // Reads the ndjson stream from /api/chat/stream: delta events update the
  // bubble as text arrives; the final result event is authoritative.
  async function streamChat(payload, signal, onDelta, onStatus) {
    const response = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });
    if (!response.ok) {
      const body = await response.json();
      const requestError = new Error(body.message || "服務暫時無法處理請求");
      requestError.status = response.status;
      throw requestError;
    }
    let result = null;
    let terminalFailure = false;
    const handleLine = (line) => {
      if (!line.trim()) return;
      let event;
      try { event = JSON.parse(line); } catch (_) { return; }
      if (event.type === "delta" && typeof event.text === "string") onDelta(event.text);
      else if (event.type === "status") onStatus?.(event);
      else if (event.type === "terminal" && event.status !== "completed") terminalFailure = true;
      else if (event.type === "result") result = event;
    };
    if (response.body?.getReader) {
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let newline;
        while ((newline = buffer.indexOf("\n")) >= 0) {
          handleLine(buffer.slice(0, newline));
          buffer = buffer.slice(newline + 1);
        }
      }
      handleLine(buffer);
    } else {
      (await response.text()).split("\n").forEach(handleLine);
    }
    if (!result || terminalFailure) throw new Error("服務暫時無法處理請求");
    return result;
  }

  async function sendMessage(event) {
    event?.preventDefault();
    const value = prompt.value.trim();
    if (!value || state.controller) return;
    const conversation = activeConversation();
    conversation.messages.push({ role: "user", content: value });
    conversation.updatedAt = new Date().toISOString();
    // 有新訊息的對話排到最上面。
    state.conversations = [conversation, ...state.conversations.filter((item) => item.id !== conversation.id)];
    if (conversation.messages.filter((item) => item.role === "user").length === 1) {
      // AI 命名要等回答結束，中間這段時間側欄不能出現一排一模一樣的標題。
      const base = value.slice(0, 24) || "新對話";
      const taken = state.conversations.filter(
        (item) => item.id !== conversation.id && String(item.title || "").startsWith(base),
      ).length;
      conversation.title = taken ? `${base}（${taken + 1}）` : base;
    }
    conversation.messages.push({ role: "assistant", content: "", loading: true });
    prompt.value = "";
    updateComposer();
    state.controller = new AbortController();
    setBusy(true);
    persist();
    render();
    startWaitHint();
    try {
      // 最近 8 題完整送出（模型脈絡與檢索用），更早的只送前 80 字，
      // 讓伺服器知道哪些題目已經問過，建議問題才不會重複。
      // 連 AI 自己說過的話一起送：不送的話模型每一輪都是失憶的，
      // 「然後呢」「再短一點」「你說錯了吧」全部接不上。
      // 伺服器只拿最後 8 則進模型脈絡，assistant 的內容另外夾長度。
      const turns = conversation.messages
        .slice(0, -2)
        .filter((item) => !item.loading && item.content && (
          item.role === "user" || (item.role === "assistant" && item.status === "answered")
        ))
        .slice(-60);
      const history = turns.map((item, index) => ({
        role: item.role,
        content: String(item.content).slice(0, index >= turns.length - 8 ? 1200 : 80),
      }));
      let streamedText = "";
      const tone = state.tone;
      // 這一次生成屬於哪一段對話。生成中切到別段時，這些字不可以寫過去——
      // 舊版直接抓畫面上最後一則訊息，A 還在生成、人切到 B，A 的字就一個一個
      // 打進 B 的泡泡裡。最終結果本來就寫回下面那個 conversation 物件，
      // 所以只要擋住畫面這一段就好。
      const streamingId = conversation.id;
      const body = await streamChat(
        { message: value, conversation_id: conversation.id, history, tone },
        state.controller.signal,
        (delta) => {
          streamedText += delta;
          // 客服模式不即時吐字：維持輸入中的點點，等結果再一句一句發——
          // 那條路的等待提示要留著跑，不能在這裡停掉。
          if (tone === "service") return;
          if (state.activeId !== streamingId) return;
          stopWaitHint();
          const textNode = messages.lastElementChild?.querySelector(".message-text");
          if (textNode) {
            textNode.textContent = streamedText;
            messages.scrollTop = messages.scrollHeight;
          }
        },
        (statusEvent) => {
          // 品質重打：字已經吐完、等待提示也停了，中間 5~15 秒完全靜止，
          // 然後整段文字突然換掉——先講一聲，秒數照樣要跳。
          if (statusEvent.stage !== "refining") return;
          if (state.activeId !== streamingId) return;
          if (tone !== "service") {
            // 吐字時 textContent 已把泡泡裡的提示節點洗掉了，補一個回去。
            const textNode = messages.lastElementChild?.querySelector(".message-text");
            if (textNode && !textNode.querySelector(".wait-hint")) {
              const hint = document.createElement("p");
              hint.className = "wait-hint";
              hint.hidden = true;
              textNode.appendChild(hint);
            }
          }
          startWaitHint(REFINE_HINTS);
        },
      );
      conversation.messages[conversation.messages.length - 1] = {
        role: "assistant",
        content: body.answer,
        status: body.status,
        reason: body.reason,
        modelStatus: body.model_status,
        tone: body.tone || tone,
        // 客服模式像真人打字：一句一句出現，每句停 1~2 秒（僅模型成功回覆時）。
        pendingReveal: (body.tone || tone) === "service" && body.status === "answered" && body.model_status === "used",
        citations: body.citations || [],
        followups: body.followups || [],
        traceId: body.trace_id,
      };
      if (body.status === "answered" && !conversation.titleEdited && !conversation.titleGenerated) {
        conversation.titleGenerated = true;
        requestConversationTitle(conversation, value, body.answer);
      }
    } catch (error) {
      if (error.status === 401) showLogin("登入已過期，請重新登入");
      conversation.messages[conversation.messages.length - 1] = {
        role: "assistant",
        content: error.name === "AbortError"
          ? "已停止這次查詢。"
          : "剛剛連線不太順，你的問題我留著了，按下面的重送就好。",
        status: "escalated",
        citations: [],
        // 連線失敗不要讓使用者重打：把原本的問題放回輸入框。
        retryPrompt: error.name === "AbortError" ? "" : value,
      };
    } finally {
      stopWaitHint();
      state.controller = null;
      setBusy(false);
      persist(conversation);
      render();
      if (state.user) loadUsage();
    }
  }

  async function sendFeedback(item, rating) {
    if (item.feedback === rating) return;
    item.feedback = rating;
    persist();
    render();
    try {
      await fetch("/api/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ trace_id: item.traceId, rating }),
      });
    } catch (_) {
      // 本地已記錄，網路失敗不打斷使用。
    }
  }

  function startRename() {
    const conversation = activeConversation();
    if (!conversation) return;
    const input = el("conversation-title-input");
    el("conversation-title").hidden = true;
    el("rename-conversation").hidden = true;
    input.hidden = false;
    input.value = conversation.title;
    input.focus();
    input.select();
  }

  function finishRename(save) {
    const input = el("conversation-title-input");
    if (input.hidden) return;
    const conversation = activeConversation();
    if (save && conversation) {
      const value = input.value.trim().slice(0, 40);
      if (value && value !== conversation.title) {
        conversation.title = value;
        conversation.titleEdited = true;
        persist();
      }
    }
    input.hidden = true;
    el("conversation-title").hidden = false;
    el("rename-conversation").hidden = false;
    render();
  }

  async function requestConversationTitle(conversation, message, answer) {
    try {
      const response = await fetch("/api/chat/title", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, answer, conversation_id: conversation.id }),
      });
      if (!response.ok) return;
      const body = await response.json();
      const title = String(body.title || "").trim().slice(0, 40);
      if (title && !conversation.titleEdited) {
        conversation.title = title;
        persist(conversation);
        if (state.activeId === conversation.id) el("conversation-title").textContent = title;
        renderSidebar();
      }
    } catch (_) {
      // The title derived from the first message stays in place.
    }
  }

  function setBusy(busy) {
    el("send-button").disabled = busy;
    el("stop-button").hidden = !busy;
    prompt.disabled = busy;
  }

  function openSources(citations, selectedIndex = 0) {
    const content = el("source-content");
    content.replaceChildren();
    citations.forEach((citation, index) => {
      const block = document.createElement("section");
      block.className = "source-block";
      if (index === selectedIndex) block.dataset.selected = "true";
      block.innerHTML = `
        <div class="source-topline"><span class="source-index">[${index + 1}]</span><span class="source-score">信心 ${Math.round((citation.score || 0) * 100)}%</span></div>
        <h3>${escapeHtml(citation.section_title || citation.title)}</h3>
        <div class="source-locator">${escapeHtml(citation.title)} · ${escapeHtml(citation.locator)}</div>
        <p class="source-excerpt">${escapeHtml(citation.text)}</p>`;
      content.append(block);
    });
    el("source-drawer").classList.add("open");
    el("source-drawer").setAttribute("aria-hidden", "false");
    el("drawer-overlay").classList.remove("clear");
    el("drawer-overlay").hidden = false;
    requestAnimationFrame(() => {
      if (selectedIndex === 0) el("source-drawer").scrollTop = 0;
      else content.querySelector('[data-selected="true"]')?.scrollIntoView({ block: "start" });
    });
  }

  function closeSources() {
    el("source-drawer").classList.remove("open");
    el("source-drawer").setAttribute("aria-hidden", "true");
    el("drawer-overlay").hidden = true;
  }

  function openSidebar() {
    el("sidebar").classList.remove("desktop-hidden");
    el("sidebar").classList.add("open");
    // 手機版是把主畫面往右推 75%：被推到右邊的那一截用「透明」層接住點擊
    // 來關閉，不壓暗畫面（壓暗會在狀態列下緣出現一條明顯交界）。
    if (window.matchMedia("(max-width: 760px)").matches) {
      el("drawer-overlay").classList.add("clear");
      el("drawer-overlay").hidden = false;
    }
  }

  function closeSidebar() {
    el("sidebar").classList.remove("open");
    el("drawer-overlay").classList.remove("clear");
    if (!el("source-drawer").classList.contains("open")) el("drawer-overlay").hidden = true;
  }

  // 左下角帳號彈窗：設定（語氣）／用量（本月用量）兩個分頁。
  function toggleAccountMenu(force) {
    const menu = el("account-menu");
    const open = force !== undefined ? force : menu.hidden;
    menu.hidden = !open;
    el("user-account").setAttribute("aria-expanded", String(open));
    if (open && state.user) loadUsage();
  }

  function updateComposer() {
    prompt.style.height = "auto";
    prompt.style.height = `${Math.min(prompt.scrollHeight, 160)}px`;
  }

  function registerServiceWorker() {
    if (!("serviceWorker" in navigator) || location.protocol !== "https:" && location.hostname !== "localhost" && location.hostname !== "127.0.0.1") return;
    navigator.serviceWorker.register("/sw.js").catch(() => {
      // Installability is a progressive enhancement; chat works without it.
    });
  }

  // 伺服器沒回應時，fetch 會一直掛著，開機畫面就會停在轉圈。
  // 開機用的請求一律給逾時，逾時就顯示登入頁與錯誤訊息。
  async function fetchWithTimeout(path, options = {}, timeoutMs = 8000) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      return await fetch(path, { ...options, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  }

  async function checkHealth() {
    try {
      const response = await fetchWithTimeout("/api/health", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error();
      applyProfile(body);
    } catch (_) {
      // The header stays quiet when the health endpoint is unavailable.
    }
  }

  function formatTwd(value) {
    return new Intl.NumberFormat("zh-TW", { maximumFractionDigits: 2 }).format(Number(value || 0));
  }

  function formatTokens(value) {
    return new Intl.NumberFormat("zh-TW").format(Number(value || 0));
  }

  async function loadUsage() {
    try {
      const response = await fetch("/api/usage", { cache: "no-store" });
      if (response.status === 401) {
        showLogin("登入已過期，請重新登入");
        return;
      }
      const body = await response.json();
      if (!response.ok) throw new Error();
      const percent = Math.min(100, Math.max(0, Number(body.progress_percent || 0)));
      el("usage-progress").style.width = `${percent}%`;
      el("usage-percent").textContent = `${percent.toFixed(percent < 10 ? 1 : 0)}%`;
      el("usage-spend").textContent = `NT$${formatTwd(body.spend_twd)} / NT$${formatTwd(body.budget_twd)}`;
      el("usage-tokens").textContent = `${formatTokens(body.total_tokens)} tokens · ${body.month}`;
      const progress = el("usage-progress").parentElement;
      progress.setAttribute("aria-valuenow", String(percent));
      progress.setAttribute("aria-valuetext", `本月已使用新台幣 ${formatTwd(body.spend_twd)} 元`);
    } catch (_) {
      el("usage-spend").textContent = "用量暫時無法取得";
      el("usage-tokens").textContent = "—";
    }
  }

  async function initializeUser(user) {
    showApp(user);
    loadTone();
    state.conversations = [];
    state.activeId = null;
    syncedOnce = false;
    loadPendingDeletes();
    load();
    render();
    updateComposer();
    await syncWithServer();
    await loadUsage();
    prompt.focus();
  }

  function mergeConversations(server, tombstones = []) {
    const deleted = new Set(tombstones.map((item) => item.id));
    const byId = new Map(server.filter((item) => !pendingDeletes.has(item.id))
      .map((item) => [item.id, { ...item, expected_rev: item.rev || 0 }]));
    state.conversations.forEach((item) => {
      if (pendingDeletes.has(item.id)) return;
      const remote = byId.get(item.id);
      if (dirty.has(item.id)) {
        if (deleted.has(item.id) || (remote && (remote.rev || 0) !== (item.expected_rev || 0))) {
          // 回覆可能在途中遺失；先讓伺服器逐筆確認相同版本重送。
          if (!remote || (remote.rev || 0) !== (item.rev || 0)) preserveConflict(item);
        }
        byId.set(item.id, item);
      } else if (remote && !deleted.has(item.id)
                 && (item.expected_rev || 0) > (remote.rev || 0)) {
        // GET 可能先讀取舊版、在新版 POST 確認後才抵達；不可回退已確認內容。
        byId.set(item.id, item);
      } else if (!remote && !deleted.has(item.id)) {
        // 舊版離線紀錄安全遷移；不存在不等於已刪除。
        if ((item.messages || []).length) {
          item.expected_rev = 0;
          item.rev = Math.max(1, item.rev || 0);
          item._dirty = true;
          dirty.add(item.id);
        }
        byId.set(item.id, item);
      }
    });
    state.conversations = [...byId.values()].sort(
      (a, b) => String(b.updatedAt || "").localeCompare(String(a.updatedAt || ""))
    );
    if (!state.conversations.some((item) => item.id === state.activeId)) {
      state.activeId = state.conversations[0]?.id || null;
    }
    if (!state.activeId) newConversation();
    render();
    persist(false);
  }

  async function syncWithServer() {
    loadPendingDeletes();
    for (const id of Array.from(pendingDeletes)) await deleteConversationOnServer(id);
    const remote = await pullConversations();
    syncedOnce = true;
    if (!remote) return;
    mergeConversations(Array.isArray(remote.conversations) ? remote.conversations : [],
      Array.isArray(remote.tombstones) ? remote.tombstones : []);
    await pushConversations(dirtyConversations());
    if (remote.prefs?.tone && remote.prefs.tone !== state.tone) setTone(remote.prefs.tone);
  }

  // 分頁回到前景時再拉一次。只在登入那一刻同步的話，PWA 開著不關就會一直看到舊的，
  // 另一台裝置新增的對話永遠等不到。
  async function refreshFromServer() {
    // 回覆進行中不要動畫面。
    if (!syncedOnce || !state.user || state.controller) return;
    await syncWithServer();
  }

  // 關掉分頁前把還沒送出的那一次補送出去：debounce 還沒到就切走的話，
  // 最後幾則訊息會只留在這台裝置上。keepalive 讓請求在分頁關掉後仍然送得出去。
  function flushSync() {
    // 沒有本機改動就什麼都不要送：重新整理時盲推會把別台的新版本蓋掉。
    if (!syncedOnce) return;
    // **要送全部還沒送出去的**，不是只送當前這一段：在 A 打完字、切到 B、
    // 然後關掉分頁的話，A 那一段就只留在這台裝置上了。
    const pending = dirtyConversations();
    if (!pending.length) return;
    clearTimeout(syncTimer);
    try {
      fetch("/api/conversations", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversations: pending }),
        keepalive: true,
      });
      // 這裡不清 dirty：keepalive 的結果看不到，先當作沒送成功，
      // 頁面若從 bfcache 回來還會再送一次（多送一次無害，少送就是掉資料）。
    } catch (_) { /* 送不出去就等下次開啟時再同步 */ }
  }

  async function login(event) {
    event.preventDefault();
    const button = el("login-button");
    button.disabled = true;
    el("login-status").textContent = "";
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          username: el("login-username").value.trim(),
          password: el("login-password").value,
        }),
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || "登入失敗");
      await initializeUser(body.user);
    } catch (error) {
      el("login-status").textContent = error.message;
      el("login-password").select();
    } finally {
      button.disabled = false;
    }
  }

  async function logout() {
    state.controller?.abort();
    try {
      await fetch("/api/auth/logout", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
      });
    } catch (_) {
      // The local login screen still locks the UI if the network is unavailable.
    }
    state.conversations = [];
    state.activeId = null;
    showLogin("已登出");
  }

  async function restoreSession() {
    try {
      const response = await fetchWithTimeout("/api/auth/me", { cache: "no-store" });
      if (!response.ok) {
        showLogin();
        return;
      }
      const body = await response.json();
      await initializeUser(body.user);
    } catch (error) {
      showLogin(
        error?.name === "AbortError"
          ? "伺服器沒有回應，請稍後再試一次"
          : "目前無法連線，請稍後再試",
      );
    }
  }

  function applyProfile(body) {
    state.profile = body.profile || "designer_coach";
    state.assistantName = body.assistant_name || "AI 輔導教練";
    state.welcomePrompts = Array.isArray(body.welcome_prompts) && body.welcome_prompts.length
      ? body.welcome_prompts.slice(0, WELCOME_PROMPT_POOL)
      : state.welcomePrompts;
    const appName = body.app_name || "LUREAI 你的智慧大腦中心";
    document.title = appName;
    prompt.placeholder = "輸入輔導問題";
    el("index-scope").textContent = "內部索引已隔離";
  }

  el("composer").addEventListener("submit", sendMessage);
  prompt.addEventListener("input", updateComposer);
  prompt.addEventListener("keydown", (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
  el("stop-button").addEventListener("click", () => state.controller?.abort());
  document.querySelectorAll("#tone-toggle .tone-option").forEach((button) => {
    button.addEventListener("click", () => {
      switchTone(button.dataset.tone);
      toggleAccountMenu(false);
    });
  });
  // 名字區是真正的 <button>，Enter／空白鍵原生就會觸發 click。
  el("user-account").addEventListener("click", () => toggleAccountMenu());
  document.querySelectorAll(".account-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".account-tab").forEach((other) => other.classList.toggle("active", other === tab));
      el("account-panel-settings").hidden = tab.dataset.tab !== "settings";
      el("account-panel-usage").hidden = tab.dataset.tab !== "usage";
    });
  });
  el("account-backdrop").addEventListener("click", () => toggleAccountMenu(false));
  el("account-close").addEventListener("click", () => toggleAccountMenu(false));
  // 右上角膠囊：跳出小彈窗確認「是否切換為◯◯模式」，確認才切換。
  const toneLabel = (tone) => (tone === "service" ? "客服模式" : "專家模式");
  const otherTone = () => (state.tone === "service" ? "expert" : "service");
  el("tone-indicator").addEventListener("click", (event) => {
    event.stopPropagation();
    const box = el("tone-confirm");
    if (!box.hidden) { box.hidden = true; return; }
    el("tone-confirm-text").textContent = `是否切換為${toneLabel(otherTone())}？`;
    box.hidden = false;
  });
  el("tone-confirm-ok").addEventListener("click", () => {
    switchTone(otherTone());
    el("tone-confirm").hidden = true;
  });
  el("tone-confirm-cancel").addEventListener("click", () => { el("tone-confirm").hidden = true; });
  document.addEventListener("click", (event) => {
    if (el("tone-confirm").hidden) return;
    if (!event.target.closest("#tone-confirm, #tone-indicator")) el("tone-confirm").hidden = true;
  });
  el("new-chat").addEventListener("click", newConversation);
  el("sidebar-search").addEventListener("click", () => {
    const search = el("conversation-search");
    search.hidden = !search.hidden;
    if (!search.hidden) el("conversation-search-input").focus();
  });
  el("conversation-search-input").addEventListener("input", renderSidebar);
  el("sidebar-panel").addEventListener("click", () => el("sidebar").classList.add("desktop-hidden"));
  el("source-close").addEventListener("click", closeSources);
  el("drawer-overlay").addEventListener("click", () => { closeSources(); closeSidebar(); });
  el("menu-button").addEventListener("click", openSidebar);
  // 手機版手勢（PWA 裡沒有瀏覽器的返回手勢，所以兩個方向都要自己做）：
  // 畫面上任何地方往右滑＝展開，往左滑＝收起。不限制起手位置——選單佔 75%，
  // 要求使用者一定要從螢幕邊緣起手（或滑到右邊那一小截才收得掉）並不合理。
  // 聊天頁沒有任何橫向捲動的容器，所以不會打架；真的有的時候用下面那道保險。
  const SWIPE_DISTANCE = 50;  // 橫向要滑超過這麼多才算數
  const SWIPE_DRIFT = 40;     // 直向偏移超過這麼多就當成在捲動，取消手勢

  // 起手落在輸入框或「自己會橫向捲動」的東西上時不要接手：前者橫滑是在移動
  // 游標，後者橫滑是在捲它自己的內容。
  function swipeBlocked(target) {
    for (let node = target; node && node !== document.body; node = node.parentElement) {
      if (!(node instanceof Element)) continue;
      const tag = node.tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return true;
      if (node.scrollWidth > node.clientWidth + 1) {
        const overflowX = getComputedStyle(node).overflowX;
        if (overflowX === "auto" || overflowX === "scroll") return true;
      }
    }
    return false;
  }

  let swipe = null;
  document.addEventListener("touchstart", (event) => {
    if (event.touches.length !== 1) return;
    if (!window.matchMedia("(max-width: 760px)").matches) return;
    if (swipeBlocked(event.target)) return;
    const touch = event.touches[0];
    swipe = { x: touch.clientX, y: touch.clientY, open: el("sidebar").classList.contains("open") };
  }, { passive: true });
  document.addEventListener("touchmove", (event) => {
    if (!swipe) return;
    const touch = event.touches[0];
    const deltaX = touch.clientX - swipe.x;
    // 直向先超過門檻就是在捲動，直接放棄這次手勢，不然捲清單會誤收選單。
    if (Math.abs(touch.clientY - swipe.y) > SWIPE_DRIFT) {
      swipe = null;
      return;
    }
    if (!swipe.open && deltaX > SWIPE_DISTANCE) {
      openSidebar();
      swipe = null;
    } else if (swipe.open && deltaX < -SWIPE_DISTANCE) {
      closeSidebar();
      swipe = null;
    }
  }, { passive: true });
  document.addEventListener("touchend", () => { swipe = null; }, { passive: true });
  el("sidebar-close").addEventListener("click", closeSidebar);
  el("login-form").addEventListener("submit", login);
  el("logout-button").addEventListener("click", logout);
  el("rename-conversation").addEventListener("click", startRename);
  el("conversation-title-input").addEventListener("keydown", (event) => {
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "Enter") { event.preventDefault(); finishRename(true); }
    if (event.key === "Escape") { event.stopPropagation(); finishRename(false); }
  });
  el("conversation-title-input").addEventListener("blur", () => finishRename(true));
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeSources(); closeSidebar(); toggleAccountMenu(false); el("tone-confirm").hidden = true; } });

  // ---- 側欄下拉更新：手機上最直覺的「我要看最新的」動作 ----
  const PULL_THRESHOLD = 64;   // 拉超過這個距離放開才更新
  const PULL_MAX = 96;         // 最多把提示推出這麼高，再拉也不會變形

  function setupPullToRefresh() {
    const list = el("conversation-list");
    const indicator = el("pull-refresh");
    if (!list || !indicator) return;
    let startY = 0;
    let distance = 0;
    let dragging = false;

    const reset = (height) => {
      indicator.classList.remove("dragging", "ready");
      indicator.style.height = height;
    };

    list.addEventListener("touchstart", (event) => {
      // 只有已經捲到最上面才接手，不然會跟正常捲動打架。
      if (list.scrollTop > 0 || indicator.classList.contains("loading")) return;
      startY = event.touches[0].clientY;
      distance = 0;
      dragging = true;
      indicator.classList.add("dragging");
    }, { passive: true });

    list.addEventListener("touchmove", (event) => {
      if (!dragging) return;
      distance = event.touches[0].clientY - startY;
      if (distance <= 0) {
        indicator.style.height = "0px";
        return;
      }
      // 阻尼：拉越遠移動越少，手感比較像原生。
      const pulled = Math.min(PULL_MAX, distance * 0.5);
      indicator.style.height = `${pulled}px`;
      indicator.classList.toggle("ready", pulled >= PULL_THRESHOLD * 0.5);
    }, { passive: true });

    list.addEventListener("touchend", async () => {
      if (!dragging) return;
      dragging = false;
      const pulled = Math.min(PULL_MAX, Math.max(0, distance) * 0.5);
      if (pulled < PULL_THRESHOLD * 0.5) {
        reset("0px");
        return;
      }
      indicator.classList.remove("dragging", "ready");
      indicator.classList.add("loading");
      indicator.style.height = "36px";
      try {
        await refreshFromServer();
      } finally {
        indicator.classList.remove("loading");
        indicator.style.height = "0px";
      }
    });
  }

  async function bootstrap() {
    // 不論健康檢查或連線發生什麼事，10 秒內一定要有畫面可以操作。
    const safety = setTimeout(() => {
      if (el("app-shell").hidden && el("login-gate").hidden) {
        showLogin("載入逾時，請重新整理再試一次");
      }
    }, 10000);
    registerServiceWorker();
    setupPullToRefresh();
    // 分頁回到前景時再同步一次；切走或關掉前把還沒送出的補送出去。
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refreshFromServer();
      else flushSync();
    });
    window.addEventListener("pagehide", flushSync);
    window.addEventListener("online", refreshFromServer);
    try {
      await checkHealth();
      await restoreSession();
    } catch (_) {
      showLogin("目前無法連線，請稍後再試");
    } finally {
      clearTimeout(safety);
    }
  }

  bootstrap();
})();
