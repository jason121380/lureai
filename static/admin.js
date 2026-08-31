(() => {
  "use strict";
  const TOKEN_KEY = "zhang-rag-admin-token";
  const el = (id) => document.getElementById(id);
  let toastTimer;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  function token() { return el("admin-token").value.trim(); }

  function showGate(message = "") {
    el("admin-shell").hidden = true;
    el("admin-gate").hidden = false;
    el("admin-gate-status").textContent = message;
    requestAnimationFrame(() => el("gate-token").focus());
  }

  function showAdmin() {
    el("admin-gate").hidden = true;
    el("admin-shell").hidden = false;
    window.lucide?.createIcons();
  }

  async function loadProfile() {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) return;
      const coaching = body.profile === "designer_coach";
      document.title = `知識庫管理｜${body.app_name}`;
      el("admin-chat-label").textContent = coaching ? "返回輔導對話" : "返回客服對話";
      el("admin-knowledge-scope").textContent = coaching
        ? "內部輔導目前可使用的來源區塊"
        : "客服目前可使用的來源區塊";
    } catch (_) {
      // Keep the static labels when the health endpoint is unavailable.
    }
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      ...options,
      headers: { "Content-Type": "application/json", "X-Admin-Token": token(), ...(options.headers || {}) },
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.message || "管理請求失敗");
    return body;
  }

  function toast(message, error = false) {
    const node = el("toast");
    node.textContent = message;
    node.className = `toast show${error ? " error" : ""}`;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { node.className = "toast"; }, 2800);
  }

  async function loadStats() {
    try {
      const body = await api("/api/admin/stats");
      el("stat-chunks").textContent = body.chunks;
      el("stat-audits").textContent = body.audits;
      el("stat-answered").textContent = body.statuses.answered || 0;
      el("stat-escalated").textContent = body.statuses.escalated || 0;
      const pipeline = body.pipeline || {};
      el("stat-source-files").textContent = pipeline.source_files ?? "—";
      el("stat-markdown-files").textContent = pipeline.markdown_files ?? "—";
      el("stat-conversation-cases").textContent = pipeline.conversation_cases ?? "—";
      el("stat-protected-files").textContent = pipeline.protected_files ?? "—";
      el("admin-status").textContent = `管理權限已驗證 · ${body.chunks} 個知識區塊`;
      return true;
    } catch (error) {
      el("admin-status").textContent = error.message;
      return false;
    }
  }

  function resultRows(items) {
    if (!items.length) return '<div class="empty-state">沒有符合的結果</div>';
    return items.map((item, index) => `
      <article class="result-row">
        <h3>[${index + 1}] ${escapeHtml(item.title)}</h3>
        <div class="result-meta"><span>${escapeHtml(item.locator)}</span><span>${Math.round((item.score || 0) * 100)}%</span></div>
        <p>${escapeHtml(item.text)}</p>
      </article>`).join("");
  }

  async function retrieve(event) {
    event.preventDefault();
    const message = el("retrieval-query").value.trim();
    if (!message) return;
    try {
      const body = await api("/api/admin/retrieve", { method: "POST", body: JSON.stringify({ message }) });
      const target = el("retrieval-results");
      target.className = "result-list";
      target.innerHTML = resultRows(body.items);
    } catch (error) { toast(error.message, true); }
  }

  async function loadKnowledge(event) {
    event?.preventDefault();
    try {
      const query = encodeURIComponent(el("knowledge-query").value.trim());
      const body = await api(`/api/admin/chunks?q=${query}`);
      const rows = body.items.map((item) => `
        <tr><td><strong>${escapeHtml(item.title)}</strong><br><span class="source-locator">${escapeHtml(item.source_file || "")}</span></td><td>${escapeHtml(item.locator)}</td><td>${escapeHtml(item.text).slice(0, 220)}</td></tr>`).join("");
      el("knowledge-results").innerHTML = rows ? `<table class="data-table"><thead><tr><th>來源</th><th>定位</th><th>內容</th></tr></thead><tbody>${rows}</tbody></table>` : '<div class="empty-state">沒有符合的知識</div>';
    } catch (error) { toast(error.message, true); }
  }

  async function loadAudits() {
    try {
      const body = await api("/api/admin/audits");
      const rows = body.items.map((item) => `
        <tr><td>${escapeHtml(new Date(item.created_at).toLocaleString("zh-TW"))}</td><td>${escapeHtml(item.question)}</td><td><span class="table-status ${escapeHtml(item.status)}">${item.status === "answered" ? "已回答" : "轉人工"}</span></td><td>${escapeHtml(item.reason || "")}</td><td>${item.top_score == null ? "—" : Math.round(item.top_score * 100) + "%"}</td></tr>`).join("");
      el("audit-results").innerHTML = rows ? `<table class="data-table"><thead><tr><th>時間</th><th>問題</th><th>狀態</th><th>原因</th><th>分數</th></tr></thead><tbody>${rows}</tbody></table>` : '<div class="empty-state">尚無查詢紀錄</div>';
    } catch (error) { toast(error.message, true); }
  }

  async function reindex() {
    const button = el("reindex-button");
    button.disabled = true;
    try {
      const body = await api("/api/admin/reindex", { method: "POST", body: "{}" });
      toast(`索引完成：匯入 ${body.imported}，拒絕 ${body.rejected}`);
      await Promise.all([loadStats(), loadKnowledge()]);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  }

  async function authenticate(suppliedToken = token()) {
    const value = String(suppliedToken || "").trim();
    if (!value) {
      showGate("請輸入管理權杖");
      return;
    }
    el("admin-token").value = value;
    el("gate-token").value = value;
    if (await loadStats()) {
      localStorage.setItem(TOKEN_KEY, value);
      showAdmin();
      await Promise.all([loadKnowledge(), loadAudits()]);
      toast("管理權限已驗證");
    } else {
      localStorage.removeItem(TOKEN_KEY);
      showGate("管理權杖無效");
    }
  }

  function logout() {
    localStorage.removeItem(TOKEN_KEY);
    el("admin-token").value = "";
    el("gate-token").value = "";
    showGate("已登出管理後台");
  }

  const savedToken = localStorage.getItem(TOKEN_KEY) || "";
  el("admin-token").value = savedToken;
  el("gate-token").value = savedToken;
  el("admin-login-form").addEventListener("submit", (event) => {
    event.preventDefault();
    authenticate(el("gate-token").value);
  });
  el("save-token").addEventListener("click", () => authenticate());
  el("admin-token").addEventListener("keydown", (event) => { if (event.key === "Enter") authenticate(); });
  el("logout-admin").addEventListener("click", logout);
  el("retrieval-form").addEventListener("submit", retrieve);
  el("knowledge-form").addEventListener("submit", loadKnowledge);
  el("refresh-audits").addEventListener("click", loadAudits);
  el("reindex-button").addEventListener("click", reindex);
  window.lucide?.createIcons();
  loadProfile();
  if (savedToken) authenticate(savedToken);
  else showGate();
})();
