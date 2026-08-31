(() => {
  "use strict";

  const STORAGE_PREFIX = "zhang-rag-conversations-v1";
  const state = {
    conversations: [], activeId: null, controller: null,
    user: null,
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

  function storageKey() {
    return `${STORAGE_PREFIX}-${state.profile}-${state.user?.id || "anonymous"}`;
  }

  function showLogin(message = "") {
    state.user = null;
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
    const conversation = { id: makeId(), title: "新對話", createdAt: new Date().toISOString(), messages: [] };
    state.conversations.unshift(conversation);
    state.activeId = conversation.id;
    persist();
    render();
    prompt.focus();
  }

  function load() {
    try {
      state.conversations = JSON.parse(localStorage.getItem(storageKey()) || "[]");
    } catch (_) {
      state.conversations = [];
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

  function renderSidebar() {
    const list = el("conversation-list");
    const query = el("conversation-search-input").value.trim().toLowerCase();
    list.replaceChildren();
    state.conversations.filter((conversation) => conversation.title.toLowerCase().includes(query)).forEach((conversation) => {
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
      <h2>我們該從哪裡開始？</h2>
      <div class="prompt-list"></div>`;
    const list = wrapper.querySelector(".prompt-list");
    state.welcomePrompts.slice(0, 3).forEach((label) => {
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
    const isEmpty = !conversation.messages.length;
    el("chat-main").classList.toggle("is-empty", isEmpty);
    if (isEmpty) messages.append(welcomeView());
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
    updateComposer();
    state.controller = new AbortController();
    setBusy(true);
    persist();
    render();
    try {
      const history = conversation.messages
        .slice(0, -2)
        .filter((item) => !item.loading && item.role === "user" && item.content)
        .slice(-8)
        .map((item) => ({ role: item.role, content: String(item.content).slice(0, 1200) }));
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: value, conversation_id: conversation.id, history }),
        signal: state.controller.signal,
      });
      const body = await response.json();
      if (!response.ok) {
        const requestError = new Error(body.message || "服務暫時無法處理請求");
        requestError.status = response.status;
        throw requestError;
      }
      conversation.messages[conversation.messages.length - 1] = {
        role: "assistant",
        content: body.answer,
        status: body.status,
        reason: body.reason,
        modelStatus: body.model_status,
        citations: body.citations || [],
        traceId: body.trace_id,
      };
    } catch (error) {
      if (error.status === 401) showLogin("登入已過期，請重新登入");
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
      if (state.user) loadUsage();
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
      status.title = `${body.chunks} 筆知識就緒`;
      status.setAttribute("aria-label", `服務狀態：${body.chunks} 筆知識就緒`);
    } catch (_) {
      status.className = "service-status offline";
      status.lastElementChild.textContent = "服務離線";
      status.title = "服務離線";
      status.setAttribute("aria-label", "服務狀態：服務離線");
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
      const response = await fetch("/api/auth/me", { cache: "no-store" });
      if (!response.ok) {
        showLogin();
        return;
      }
      const body = await response.json();
      await initializeUser(body.user);
    } catch (_) {
      showLogin("目前無法連線，請稍後再試");
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
    if (event.isComposing || event.keyCode === 229) return;
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  });
  el("stop-button").addEventListener("click", () => state.controller?.abort());
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
  document.addEventListener("keydown", (event) => { if (event.key === "Escape") { closeSources(); closeSidebar(); } });

  async function bootstrap() {
    await checkHealth();
    await restoreSession();
  }

  bootstrap();
})();
