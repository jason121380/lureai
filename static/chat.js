(() => {
  "use strict";

  const STORAGE_PREFIX = "zhang-rag-conversations-v1";
  const state = {
    conversations: [], activeId: null, controller: null,
    profile: "customer_service",
    assistantName: "AI 客服",
    welcomePrompts: [
      "顧客不滿意怎麼處理？",
      "臉型可以直接決定髮型嗎？",
      "預約需要提供什麼資訊？",
      "染髮多少錢？",
    ],
  };
  const el = (id) => document.getElementById(id);
  const messages = el("messages");
  const prompt = el("prompt");

  function makeId() {
    return globalThis.crypto?.randomUUID?.() || `chat-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function newConversation() {
    const conversation = { id: makeId(), title: "新對話", createdAt: new Date().toISOString(), messages: [] };
    state.conversations.unshift(conversation);
    state.activeId = conversation.id;
    persist();
    render();
    prompt.focus();
  }

  function load() {
    try {
      state.conversations = JSON.parse(localStorage.getItem(`${STORAGE_PREFIX}-${state.profile}`) || "[]");
    } catch (_) {
      state.conversations = [];
    }
    if (!Array.isArray(state.conversations) || !state.conversations.length) newConversation();
    else state.activeId = state.conversations[0].id;
  }

  function persist() {
    localStorage.setItem(`${STORAGE_PREFIX}-${state.profile}`, JSON.stringify(state.conversations.slice(0, 30)));
  }

  function activeConversation() {
    return state.conversations.find((item) => item.id === state.activeId);
  }

  function renderSidebar() {
    const list = el("conversation-list");
    list.replaceChildren();
    state.conversations.forEach((conversation) => {
      const button = document.createElement("button");
      button.className = `conversation-item${conversation.id === state.activeId ? " active" : ""}`;
      button.type = "button";
      button.innerHTML = '<i data-lucide="message-square"></i>';
      const label = document.createElement("span");
      label.textContent = conversation.title;
      button.append(label);
      button.addEventListener("click", () => {
        state.activeId = conversation.id;
        render();
        closeSidebar();
      });
      list.append(button);
    });
  }

  function welcomeView() {
    const wrapper = document.createElement("div");
    wrapper.className = "welcome";
    wrapper.innerHTML = `
      <div class="welcome-mark"><i data-lucide="message-circle-more"></i></div>
      <h2>今天想查詢什麼？</h2>
      <p>系統只會使用已核准知識回答，並提供可追溯來源。</p>
      <div class="prompt-grid"></div>`;
    const grid = wrapper.querySelector(".prompt-grid");
    state.welcomePrompts.forEach((label) => {
      const button = document.createElement("button");
      button.className = "prompt-chip";
      button.type = "button";
      button.textContent = label;
      grid.append(button);
    });
    wrapper.querySelectorAll(".prompt-chip").forEach((button) => button.addEventListener("click", () => {
      prompt.value = button.textContent;
      updateComposer();
      prompt.focus();
    }));
    return wrapper;
  }

  function messageView(item) {
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
    } else {
      text.textContent = item.content;
    }
    content.append(role, text);

    if (item.status) {
      const status = document.createElement("div");
      status.className = `message-status ${item.status}`;
      const answered = item.status === "answered";
      status.innerHTML = `<i data-lucide="${answered ? "badge-check" : "user-round-check"}"></i><span>${answered ? "已根據知識庫回答" : "需要人工協助"}</span>`;
      content.append(status);
    }

    if (item.citations?.length) {
      const citations = document.createElement("div");
      citations.className = "citation-list";
      item.citations.slice(0, 6).forEach((citation, index) => {
        const button = document.createElement("button");
        button.className = "citation-button";
        button.type = "button";
        button.innerHTML = `<span class="citation-number">${index + 1}</span><span>${escapeHtml(citation.title)}</span>`;
        button.addEventListener("click", () => openSources(item.citations, index));
        citations.append(button);
      });
      content.append(citations);
    }
    row.append(avatar, content);
    return row;
  }

  function renderMessages() {
    const conversation = activeConversation();
    messages.replaceChildren();
    if (!conversation.messages.length) messages.append(welcomeView());
    else conversation.messages.forEach((item) => messages.append(messageView(item)));
    el("conversation-title").textContent = conversation.title;
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
    state.controller = new AbortController();
    setBusy(true);
    persist();
    render();
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: value, conversation_id: conversation.id }),
        signal: state.controller.signal,
      });
      const body = await response.json();
      if (!response.ok) throw new Error(body.message || "服務暫時無法處理請求");
      conversation.messages[conversation.messages.length - 1] = {
        role: "assistant",
        content: body.answer,
        status: body.status,
        reason: body.reason,
        citations: body.citations || [],
        traceId: body.trace_id,
      };
    } catch (error) {
      conversation.messages[conversation.messages.length - 1] = {
        role: "assistant",
        content: error.name === "AbortError" ? "已停止這次查詢。" : "目前無法連線到客服知識庫，請稍後再試。",
        status: "escalated",
        citations: [],
      };
    } finally {
      state.controller = null;
      setBusy(false);
      persist();
      render();
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
        <h3>${escapeHtml(citation.title)}</h3>
        <div class="source-locator">${escapeHtml(citation.source_file)} · ${escapeHtml(citation.locator)}</div>
        <p class="source-excerpt">${escapeHtml(citation.text)}</p>`;
      content.append(block);
    });
    el("source-drawer").classList.add("open");
    el("source-drawer").setAttribute("aria-hidden", "false");
    el("drawer-overlay").hidden = false;
    requestAnimationFrame(() => content.querySelector('[data-selected="true"]')?.scrollIntoView({ block: "start" }));
  }

  function closeSources() {
    el("source-drawer").classList.remove("open");
    el("source-drawer").setAttribute("aria-hidden", "true");
    el("drawer-overlay").hidden = true;
  }

  function openSidebar() {
    el("sidebar").classList.add("open");
    el("drawer-overlay").hidden = false;
  }

  function closeSidebar() {
    el("sidebar").classList.remove("open");
    if (!el("source-drawer").classList.contains("open")) el("drawer-overlay").hidden = true;
  }

  function updateComposer() {
    prompt.style.height = "auto";
    prompt.style.height = `${Math.min(prompt.scrollHeight, 160)}px`;
    el("char-count").textContent = `${prompt.value.length} / 1200`;
  }

  async function checkHealth() {
    const status = el("service-status");
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) throw new Error();
      applyProfile(body);
      status.className = "service-status online";
      status.lastElementChild.textContent = `${body.chunks} 筆知識就緒`;
    } catch (_) {
      status.className = "service-status offline";
      status.lastElementChild.textContent = "服務離線";
    }
  }

  function applyProfile(body) {
    state.profile = body.profile || "customer_service";
    state.assistantName = body.assistant_name || "AI 客服";
    state.welcomePrompts = Array.isArray(body.welcome_prompts) && body.welcome_prompts.length
      ? body.welcome_prompts.slice(0, 4)
      : state.welcomePrompts;
    const appName = body.app_name || "張副總 AI 客服";
    document.title = appName;
    el("brand-title").textContent = state.profile === "designer_coach" ? "設計師輔導台" : "客服知識台";
    el("app-subtitle").textContent = appName;
    prompt.placeholder = state.profile === "designer_coach" ? "輸入輔導問題" : "輸入客服問題";
    el("knowledge-scope").textContent = state.profile === "designer_coach"
      ? "回答僅使用已核准內部輔導知識"
      : "回答僅使用已核准客服知識";
    el("index-scope").textContent = state.profile === "designer_coach"
      ? "內部索引已隔離"
      : "客服索引已隔離";
  }

  el("composer").addEventListener("submit", sendMessage);
  prompt.addEventListener("input", updateComposer);
  prompt.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
  el("stop-button").addEventListener("click", () => state.controller?.abort());
  el("new-chat").addEventListener("click", newConversation);
  el("source-close").addEventListener("click", closeSources);
  el("drawer-overlay").addEventListener("click", () => { closeSources(); closeSidebar(); });
  el("menu-button").addEventListener("click", openSidebar);
  el("sidebar-close").addEventListener("click", closeSidebar);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeSources(); closeSidebar(); } });

  async function bootstrap() {
    await checkHealth();
    load();
    render();
    updateComposer();
  }

  bootstrap();
})();
