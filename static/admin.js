(() => {
  "use strict";
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

  const healthStatus = {
    ok: { label: "正常", icon: "circle-check" },
    warning: { label: "警告", icon: "triangle-alert" },
    error: { label: "錯誤", icon: "circle-x" },
  };

  const detailLabels = {
    profile: "Profile", python: "Python", uptime_seconds: "運行秒數",
    admin_auth: "管理驗證", max_request_bytes: "請求上限",
    assets: "靜態資源", missing: "缺少資源", empty: "空白資源",
    unreadable: "無法讀取", invalid: "結構異常", bytes: "資源大小",
    integrity: "完整性", writable: "可寫入", size_bytes: "檔案大小",
    chunks: "知識區塊", fts_chunks: "FTS 區塊", probe_hits: "探針命中",
    records: "來源筆數", invalid_records: "無效資料",
    mode: "回答模式", configured: "設定", model: "模型", provider_host: "服務主機",
    service_chain: "服務鏈", unavailable: "未就緒",
    approved_records: "核准筆數", duplicate_chunk_ids: "重複 ID", indexed_records: "索引筆數",
    missing_from_index: "索引缺少", extra_in_index: "索引多出", changed_records: "內容變更", in_sync: "同步",
    error_type: "錯誤類型",
    users: "使用者", active_users: "啟用帳號", sessions: "Sessions",
    password_storage: "密碼儲存", session_storage: "Session 儲存",
  };

  function healthDetail(value) {
    if (typeof value === "boolean") return value ? "是" : "否";
    if (Array.isArray(value)) return value.length ? value.join("、") : "無";
    if (value && typeof value === "object") {
      return Object.entries(value).map(([key, item]) => `${key}: ${healthDetail(item)}`).join(" · ");
    }
    return String(value ?? "—");
  }

  function renderHealth(body) {
    const state = healthStatus[body.status] || healthStatus.error;
    const overview = el("health-overall").closest(".health-overview");
    overview.dataset.status = body.status;
    overview.querySelector(".health-state-icon").innerHTML = `<i data-lucide="${state.icon}"></i>`;
    el("health-overall").textContent = state.label;
    el("health-summary").innerHTML = `
      <span><strong>${body.summary.ok}</strong> 正常</span>
      <span><strong>${body.summary.warning}</strong> 警告</span>
      <span><strong>${body.summary.error}</strong> 錯誤</span>`;
    el("health-checked-at").textContent = `最後檢查：${new Date(body.checked_at).toLocaleString("zh-TW")}`;
    el("health-grid").innerHTML = body.checks.map((item) => {
      const itemState = healthStatus[item.status] || healthStatus.error;
      const details = Object.entries(item.details || {}).map(([key, value]) => `
        <span><b>${escapeHtml(detailLabels[key] || key)}</b>${escapeHtml(healthDetail(value))}</span>`).join("");
      return `<article class="health-item" data-status="${escapeHtml(item.status)}">
        <span class="health-item-icon"><i data-lucide="${itemState.icon}"></i></span>
        <div class="health-item-copy"><div><strong>${escapeHtml(item.label)}</strong><span class="health-badge">${itemState.label}</span></div><p>${escapeHtml(item.message)}</p></div>
        <div class="health-item-meta"><span class="health-latency">${item.latency_ms} ms</span><div>${details}</div></div>
      </article>`;
    }).join("");
    window.lucide?.createIcons();
  }

  async function loadHealth(showNotification = false) {
    const button = el("refresh-health");
    button.disabled = true;
    button.classList.add("is-loading");
    try {
      const body = await api("/api/admin/health");
      renderHealth(body);
      if (showNotification) toast("系統健康檢查完成", body.status === "error");
      return true;
    } catch (error) {
      const overview = el("health-overall").closest(".health-overview");
      overview.querySelector(".health-state-icon").innerHTML = '<i data-lucide="circle-x"></i>';
      el("health-overall").textContent = "檢查失敗";
      overview.dataset.status = "error";
      el("health-summary").innerHTML = '<span><strong>—</strong> 正常</span><span><strong>—</strong> 警告</span><span><strong>—</strong> 錯誤</span>';
      el("health-checked-at").textContent = `檢查失敗：${new Date().toLocaleString("zh-TW")}`;
      el("health-grid").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
      window.lucide?.createIcons();
      if (showNotification) toast(error.message, true);
      return false;
    } finally {
      button.disabled = false;
      button.classList.remove("is-loading");
    }
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

  async function loadUsers() {
    try {
      const body = await api("/api/admin/users");
      const rows = body.items.map((item) => `
        <tr><td><strong>${escapeHtml(item.username)}</strong></td><td>${item.active ? "啟用" : "停用"}</td><td>${escapeHtml(new Date(item.updated_at).toLocaleString("zh-TW"))}</td></tr>`).join("");
      el("user-results").innerHTML = rows
        ? `<table class="data-table"><thead><tr><th>帳號</th><th>狀態</th><th>最後更新</th></tr></thead><tbody>${rows}</tbody></table>`
        : '<div class="empty-state">尚未建立使用者帳號</div>';
    } catch (error) {
      el("user-results").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    }
  }

  async function saveUser(event) {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const body = await api("/api/admin/users", {
        method: "POST",
        body: JSON.stringify({
          username: el("user-username").value.trim(),
          password: el("user-password").value,
        }),
      });
      el("user-username").value = "";
      el("user-password").value = "";
      toast(`帳號 ${body.user.username} 已建立或重設`);
      await loadUsers();
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function reindex() {
    const button = el("reindex-button");
    button.disabled = true;
    try {
      const body = await api("/api/admin/reindex", { method: "POST", body: "{}" });
      toast(`索引完成：匯入 ${body.imported}，拒絕 ${body.rejected}`);
      await Promise.all([loadStats(), loadKnowledge(), loadHealth()]);
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
      showAdmin();
      await Promise.all([loadKnowledge(), loadAudits(), loadHealth(), loadUsers()]);
      toast("管理權限已驗證");
    } else {
      showGate("管理權杖無效");
    }
  }

  function logout() {
    el("admin-token").value = "";
    el("gate-token").value = "";
    showGate("已登出管理後台");
  }

  el("admin-token").value = "";
  el("gate-token").value = "";
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
  el("refresh-health").addEventListener("click", () => loadHealth(true));
  el("reindex-button").addEventListener("click", reindex);
  el("user-form").addEventListener("submit", saveUser);
  window.lucide?.createIcons();
  loadProfile();
  showGate();
})();
