(() => {
  "use strict";

  const STORAGE_PREFIX = "zhang-rag-conversations-v1";
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
      state.activeId = empty.id;
    } else {
      const conversation = { id: makeId(), title: "新對話", createdAt: new Date().toISOString(), messages: [] };
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
    if (Array.isArray(state.conversations)) {
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

  function persistenceSnapshot(conversationLimit, messageLimit, contentLimit, citationTextLimit) {
    return state.conversations.slice(0, conversationLimit).map((conversation) => ({
      ...conversation,
      messages: (conversation.messages || []).slice(-messageLimit).map((message) => ({
        ...message,
        content: String(message.content || "").slice(0, contentLimit),
        citations: (message.citations || []).slice(0, 6).map((citation) => ({
          ...citation,
          text: String(citation.text || "").slice(0, citationTextLimit),
        })),
      })),
    }));
  }

  function persist() {
    const key = storageKey();
    const limits = [
      [20, 8, 8000, 600],
      [10, 6, 5000, 300],
      [3, 4, 3000, 160],
    ];
    for (const limit of limits) {
      try {
        localStorage.setItem(key, JSON.stringify(persistenceSnapshot(...limit)));
        return true;
      } catch (_) {
        // The active conversation remains usable when browser storage is full or unavailable.
      }
    }
    return false;
  }

  function activeConversation() {
    return state.conversations.find((item) => item.id === state.activeId);
  }

  function deleteConversation(id) {
    state.conversations = state.conversations.filter((conversation) => conversation.id !== id);
    if (!state.conversations.length) {
      newConversation();
      return;
    }
    if (state.activeId === id) state.activeId = state.conversations[0].id;
    persist();
    render();
  }

  function renderSidebar() {
    const list = el("conversation-list");
    const query = el("conversation-search-input").value.trim().toLowerCase();
    list.replaceChildren();
    state.conversations.filter((conversation) => conversation.title.toLowerCase().includes(query)).forEach((conversation) => {
      const item = document.createElement("div");
      item.className = `conversation-item${conversation.id === state.activeId ? " active" : ""}`;
      item.setAttribute("role", "button");
      item.tabIndex = 0;
      const label = document.createElement("span");
      label.textContent = conversation.title;
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
      item.append(label, remove);
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
      <h2>我們該從哪裡開始？</h2>
      <div class="prompt-list"></div>`;
    const list = wrapper.querySelector(".prompt-list");
    // 每次進到空白對話都隨機換一組題目。
    pickRandom(state.welcomePrompts, 3).forEach((label) => {
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
      text.innerHTML = '<div class="typing" aria-label="正在查詢"><span></span><span></span><span></span></div>';
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

    if (item.status) {
      const status = document.createElement("div");
      const answered = item.status === "answered";
      // 模型沒回應時走的是知識原文，要講清楚，不然看起來像 AI 亂答。
      const degraded = answered && item.modelStatus && !["used", "not_configured"].includes(item.modelStatus);
      status.className = `message-status ${degraded ? "degraded" : item.status}`;
      const label = answered
        ? (degraded ? `模型未回應，以下為知識原文（${escapeHtml(item.modelStatus)}）` : "已根據知識庫回答")
        : "需要人工協助";
      const icon = answered ? (degraded ? "triangle-alert" : "badge-check") : "user-round-check";
      status.innerHTML = `<i data-lucide="${icon}"></i><span>${label}</span>`;
      content.append(status);
    }

    // 每則回答都能評分（👍👎），回饋存進伺服器供之後加強知識。
    if (item.role === "assistant" && item.status === "answered" && item.traceId && !item.loading) {
      const feedback = document.createElement("div");
      feedback.className = "feedback-row";
      [["up", "thumbs-up", "這則回答有幫助"], ["down", "thumbs-down", "這則回答沒幫助"]].forEach(([rating, icon, label]) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = `feedback-button${item.feedback === rating ? " selected" : ""}`;
        button.title = label;
        button.setAttribute("aria-label", label);
        button.innerHTML = `<i data-lucide="${icon}"></i>`;
        button.addEventListener("click", () => sendFeedback(item, rating));
        feedback.append(button);
      });
      content.append(feedback);
    }

    if (item.citations?.length) {
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

  // 客服模式像真人打字：第一句直接出現，之後每句停 1~2 秒再發，
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
        setTimeout(step, 1000 + Math.random() * 1000);
        return;
      }
      typing.remove();
      extras.forEach((extra) => { extra.hidden = false; });
      messages.scrollTop = messages.scrollHeight;
      window.lucide?.createIcons();
    };
    step();
  }

  function renderMessages() {
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

    const flushParagraph = () => {
      if (paragraph.length) blocks.push(`<p>${paragraph.join("<br>")}</p>`);
      paragraph = [];
    };
    const flushList = () => {
      if (list) blocks.push(`<${list.tag}>${list.items.join("")}</${list.tag}>`);
      list = null;
    };

    for (const rawLine of String(content || "").split("\n")) {
      const line = rawLine.trimEnd();
      if (!line.trim()) {
        flushParagraph();
        flushList();
        continue;
      }
      const heading = line.match(HEADING_LINE);
      if (heading) {
        flushParagraph();
        flushList();
        blocks.push(`<p class="md-heading">${inlineMarkup(heading[1], citationCount)}</p>`);
        continue;
      }
      const bullet = line.match(BULLET_LINE);
      const ordered = bullet ? null : line.match(ORDERED_LINE);
      if (bullet || ordered) {
        flushParagraph();
        const tag = bullet ? "ul" : "ol";
        if (!list || list.tag !== tag) {
          flushList();
          list = { tag, items: [] };
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

  // 客服模式：像真人一句一句發訊息。
  // - 引用編號只給系統核對，句尾不顯示 [1]——來源照樣列在泡泡下方。
  // - 標點一律拿掉，以空白分段，像平常打字（「～」保留）。
  // - 依語意斷句：模型的每一行就是一則訊息，不做字數硬拆（硬拆會把句子切壞）。
  function serviceSentences(content) {
    return String(content || "")
      .split("\n")
      .map((line) => line.trim().replace(/^(?:[-*•]|\d{1,2}[.)])\s+/, ""))
      .map((line) => line.replace(/\s*\[\d{1,2}\]/g, ""))
      .map((line) => line.replace(/[，。、；：！？!?；「」『』（）()]/g, " "))
      .map((line) => line.replace(/\s+/g, " ").trim())
      .filter(Boolean);
  }

  function renderServiceBubbles(content) {
    return serviceSentences(content)
      .map((line) => `<p class="chat-line">${inlineMarkup(line, 0)}</p>`)
      .join("");
  }

  // Reads the ndjson stream from /api/chat/stream: delta events update the
  // bubble as text arrives; the final result event is authoritative.
  async function streamChat(payload, signal, onDelta) {
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
    const handleLine = (line) => {
      if (!line.trim()) return;
      let event;
      try { event = JSON.parse(line); } catch (_) { return; }
      if (event.type === "delta" && typeof event.text === "string") onDelta(event.text);
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
    if (!result) throw new Error("服務暫時無法處理請求");
    return result;
  }

  async function sendMessage(event) {
    event?.preventDefault();
    const value = prompt.value.trim();
    if (!value || state.controller) return;
    const conversation = activeConversation();
    conversation.messages.push({ role: "user", content: value });
    if (conversation.messages.filter((item) => item.role === "user").length === 1) {
      conversation.title = value.slice(0, 24) || "新對話";
    }
    conversation.messages.push({ role: "assistant", content: "", loading: true });
    prompt.value = "";
    updateComposer();
    state.controller = new AbortController();
    setBusy(true);
    persist();
    render();
    try {
      // 最近 8 題完整送出（模型脈絡與檢索用），更早的只送前 80 字，
      // 讓伺服器知道哪些題目已經問過，建議問題才不會重複。
      const asked = conversation.messages
        .slice(0, -2)
        .filter((item) => !item.loading && item.role === "user" && item.content)
        .slice(-60);
      const history = asked.map((item, index) => ({
        role: item.role,
        content: String(item.content).slice(0, index >= asked.length - 8 ? 1200 : 80),
      }));
      let streamedText = "";
      const tone = state.tone;
      const body = await streamChat(
        { message: value, conversation_id: conversation.id, history, tone },
        state.controller.signal,
        (delta) => {
          streamedText += delta;
          // 客服模式不即時吐字：維持輸入中的點點，等結果再一句一句發。
          if (tone === "service") return;
          const textNode = messages.lastElementChild?.querySelector(".message-text");
          if (textNode) {
            textNode.textContent = streamedText;
            messages.scrollTop = messages.scrollHeight;
          }
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
        content: error.name === "AbortError" ? "已停止這次查詢。" : "目前無法連線到知識庫，請稍後再試。",
        status: "escalated",
        citations: [],
      };
    } finally {
      state.controller = null;
      setBusy(false);
      persist();
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
        persist();
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
    // 手機版選單佔左側 80%：右邊露出的區域用「透明」層接住點擊來關閉，
    // 不壓暗畫面（壓暗會在狀態列下緣出現一條明顯交界）。
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
    load();
    render();
    updateComposer();
    await loadUsage();
    prompt.focus();
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
    state.profile = body.profile || "customer_service";
    state.assistantName = body.assistant_name || "AI 輔導教練";
    state.welcomePrompts = Array.isArray(body.welcome_prompts) && body.welcome_prompts.length
      ? body.welcome_prompts.slice(0, 12)
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
    button.addEventListener("click", () => setTone(button.dataset.tone));
  });
  el("user-account").addEventListener("click", (event) => {
    if (event.target.closest("#admin-link, #logout-button")) return;
    toggleAccountMenu();
  });
  el("user-account").addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleAccountMenu(); }
  });
  document.querySelectorAll(".account-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".account-tab").forEach((other) => other.classList.toggle("active", other === tab));
      el("account-panel-settings").hidden = tab.dataset.tab !== "settings";
      el("account-panel-usage").hidden = tab.dataset.tab !== "usage";
    });
  });
  el("account-backdrop").addEventListener("click", () => toggleAccountMenu(false));
  el("account-close").addEventListener("click", () => toggleAccountMenu(false));
  el("tone-indicator").addEventListener("click", () => {
    document.querySelector('.account-tab[data-tab="settings"]')?.click();
    toggleAccountMenu(true);
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
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeSources(); closeSidebar(); toggleAccountMenu(false); } });

  async function bootstrap() {
    // 不論健康檢查或連線發生什麼事，10 秒內一定要有畫面可以操作。
    const safety = setTimeout(() => {
      if (el("app-shell").hidden && el("login-gate").hidden) {
        showLogin("載入逾時，請重新整理再試一次");
      }
    }, 10000);
    registerServiceWorker();
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
