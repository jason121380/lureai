(() => {
  "use strict";
  const el = (id) => document.getElementById(id);
  let toastTimer;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  const SECTIONS = ["overview", "knowledge", "quality", "users", "health"];

  function showSection(id) {
    const target = SECTIONS.includes(id) ? id : "overview";
    SECTIONS.forEach((section) => { el(section).hidden = section !== target; });
    document.querySelectorAll(".admin-nav-links a").forEach((link) => {
      link.classList.toggle("active", link.getAttribute("href") === `#${target}`);
    });
    window.scrollTo(0, 0);
  }

  function showAdmin() {
    el("admin-shell").hidden = false;
    showSection(location.hash.replace("#", ""));
    enhanceSelects();
    window.lucide?.createIcons();
  }

  async function loadProfile() {
    try {
      const response = await fetch("/api/health", { cache: "no-store" });
      const body = await response.json();
      if (!response.ok) return;
      document.title = `知識庫管理｜${body.app_name}`;
    } catch (_) {
      // Keep the static labels when the health endpoint is unavailable.
    }
  }

  // The native <select> opens an OS picker that covers the page (and on iOS the
  // page behind it stops repainting, so a list still loading looks stuck).
  // Each select stays in the DOM as the source of truth; this renders it.
  function enhanceSelect(select) {
    if (!select || select.dataset.enhanced) return;
    select.dataset.enhanced = "true";
    const options = Array.from(select.options).map((option) => ({ value: option.value, label: option.textContent }));
    const wrapper = document.createElement("div");
    wrapper.className = "select-field";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "select-button";
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    if (select.getAttribute("aria-label")) button.setAttribute("aria-label", select.getAttribute("aria-label"));
    const label = document.createElement("span");
    const caret = document.createElement("i");
    caret.dataset.lucide = "chevron-down";
    button.append(label, caret);
    const menu = document.createElement("div");
    menu.className = "select-menu";
    menu.setAttribute("role", "listbox");
    menu.hidden = true;

    const paint = () => {
      const current = options.find((option) => option.value === select.value) || options[0];
      label.textContent = current ? current.label : "";
      menu.querySelectorAll("button").forEach((item) => {
        item.classList.toggle("is-selected", item.dataset.value === select.value);
      });
    };
    const close = () => {
      menu.hidden = true;
      wrapper.classList.remove("is-open");
      button.setAttribute("aria-expanded", "false");
    };
    const open = () => {
      document.querySelectorAll(".select-field.is-open").forEach((other) => {
        if (other !== wrapper) {
          other.classList.remove("is-open");
          other.querySelector(".select-menu").hidden = true;
          other.querySelector(".select-button").setAttribute("aria-expanded", "false");
        }
      });
      menu.hidden = false;
      wrapper.classList.add("is-open");
      button.setAttribute("aria-expanded", "true");
    };

    options.forEach((option) => {
      const item = document.createElement("button");
      item.type = "button";
      item.setAttribute("role", "option");
      item.dataset.value = option.value;
      item.textContent = option.label;
      item.addEventListener("click", () => {
        select.value = option.value;
        paint();
        close();
        select.dispatchEvent(new Event("change", { bubbles: true }));
      });
      menu.append(item);
    });

    button.addEventListener("click", (event) => {
      event.stopPropagation();
      if (menu.hidden) open(); else close();
    });
    document.addEventListener("click", (event) => {
      if (!wrapper.contains(event.target)) close();
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") close();
    });

    select.hidden = true;
    select.tabIndex = -1;
    select.parentNode.insertBefore(wrapper, select);
    wrapper.append(button, menu, select);
    paint();
    select.addEventListener("change", paint);
  }

  function enhanceSelects() {
    document.querySelectorAll("select").forEach(enhanceSelect);
    window.lucide?.createIcons();
  }

  async function api(path, options = {}) {
    const { timeoutMs = 20000, ...init } = options;
    // Without this a hung request leaves the panel on 「載入中…」 for ever.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response;
    try {
      response = await fetch(path, {
        ...init,
        signal: controller.signal,
        headers: { "Content-Type": "application/json", ...(init.headers || {}) },
      });
    } catch (error) {
      throw new Error(error.name === "AbortError" ? "連線逾時，請重新整理再試一次" : "連線失敗，請檢查網路");
    } finally {
      clearTimeout(timer);
    }
    const body = await response.json();
    if (response.status === 401) {
      window.location.replace("/");
      throw new Error(body.message || "請先以管理者帳號登入");
    }
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
      const composition = body.composition || {};
      const origins = composition.origins || {};
      el("stat-chunks").textContent = body.chunks;
      el("stat-sources").textContent = composition.source_files ?? "—";
      el("stat-custom").textContent = origins.custom || 0;
      el("admin-status").textContent = `${body.chunks} 個知識區塊 · ${composition.source_files || 0} 份來源`;
      const domains = composition.domains || [];
      el("domain-grid").innerHTML = domains.length
        ? domains.map((domain) => `
            <section class="domain-block">
              <header class="domain-head">
                <h4>${escapeHtml(domain.label)}</h4>
                <span>${domain.count} 塊</span>
              </header>
              <div class="category-grid">${
                (domain.categories || []).length
                  ? domain.categories.map((item) => `
                      <div class="category-card"><span>${escapeHtml(item.name)}</span><strong>${item.count}</strong></div>`).join("")
                  : '<div class="empty-state">這個主題還沒有知識</div>'
              }</div>
            </section>`).join("")
        : '<div class="empty-state">尚無知識</div>';
      return true;
    } catch (error) {
      el("admin-status").textContent = error.message;
      return false;
    }
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

  function knowledgeCard(item) {
    const custom = item.origin === "custom";
    const title = item.section_title || item.title || "（無標題）";
    const actions = custom
      ? `<button class="icon-button bordered-icon" data-edit="${escapeHtml(item.chunk_id)}" title="編輯" aria-label="編輯"><i data-lucide="pencil"></i></button>
         <button class="icon-button bordered-icon" data-remove="${escapeHtml(item.chunk_id)}" title="刪除" aria-label="刪除"><i data-lucide="trash-2"></i></button>`
      : "";
    return `<article class="knowledge-card">
      <div class="knowledge-head">
        <div>
          <h3>${escapeHtml(title)}</h3>
          <div class="knowledge-meta">
            <span class="domain-badge">${escapeHtml(domainLabels[item.domain] || "未分主題")}</span>
            <span class="origin-badge${custom ? " is-custom" : ""}">${custom ? "後台新增" : "匯入知識"}</span>
            <span>${escapeHtml(item.category || "未分類")}</span>
            <span class="source-locator">${escapeHtml(item.locator || "")}</span>
          </div>
        </div>
        <div class="knowledge-actions">${actions}</div>
      </div>
      <p>${escapeHtml(String(item.text || "").slice(0, 260))}${String(item.text || "").length > 260 ? "…" : ""}</p>
    </article>`;
  }

  const domainLabels = {
    operations: "店務營運管理",
    coaching: "設計師一對一行銷輔導",
  };

  let knowledgeCache = [];

  async function loadKnowledge(event) {
    event?.preventDefault();
    try {
      el("knowledge-results").innerHTML = '<div class="empty-state">載入中…</div>';
      const query = encodeURIComponent(el("knowledge-query")?.value.trim() || "");
      const origin = encodeURIComponent(el("knowledge-origin")?.value || "");
      const domain = encodeURIComponent(el("knowledge-domain")?.value || "");
      const body = await api(`/api/admin/chunks?q=${query}&origin=${origin}&domain=${domain}`, { timeoutMs: 15000 });
      knowledgeCache = body.items || [];
      el("knowledge-results").innerHTML = knowledgeCache.length
        ? `<div class="knowledge-count">共 ${knowledgeCache.length} 則</div>` + knowledgeCache.map(knowledgeCard).join("")
        : '<div class="empty-state">沒有符合的知識</div>';
      window.lucide?.createIcons();
    } catch (error) {
      el("knowledge-results").innerHTML = `<div class="empty-state">載入失敗：${escapeHtml(error.message)}
        <button type="button" class="command-button ghost-button" data-retry-knowledge>重新載入</button></div>`;
      el("knowledge-results").querySelector("[data-retry-knowledge]")?.addEventListener("click", () => loadKnowledge());
      toast(error.message, true);
    }
  }

  async function openEditor(chunk) {
    el("editor-chunk-id").value = chunk?.chunk_id || "";
    el("editor-title").value = chunk?.section_title || "";
    el("editor-category").value = chunk?.category || "";
    el("editor-domain").value = chunk?.domain || el("knowledge-domain").value || "coaching";
    let text = chunk?.text || "";
    if (chunk?.chunk_id) {
      // The list only carries an excerpt; fetch the full text before editing.
      try {
        const body = await api(`/api/admin/knowledge/detail?chunk_id=${encodeURIComponent(chunk.chunk_id)}`);
        text = body.chunk.text;
      } catch (_) {
        toast("讀取完整內容失敗，顯示的是摘要", true);
      }
    }
    el("editor-text").value = text;
    el("knowledge-editor").hidden = false;
    el("editor-title").focus();
  }

  function closeEditor() {
    el("knowledge-editor").hidden = true;
    el("knowledge-editor").reset();
    el("editor-chunk-id").value = "";
  }

  async function saveKnowledge(event) {
    event.preventDefault();
    const button = event.currentTarget.querySelector("button[type=submit]");
    button.disabled = true;
    try {
      const body = await api("/api/admin/knowledge", {
        method: "POST",
        body: JSON.stringify({
          chunk_id: el("editor-chunk-id").value || undefined,
          section_title: el("editor-title").value,
          category: el("editor-category").value,
          domain: el("editor-domain").value,
          text: el("editor-text").value,
        }),
      });
      toast(`已儲存：${body.chunk.section_title}`);
      closeEditor();
      await Promise.all([loadKnowledge(), loadStats()]);
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  async function removeKnowledge(chunkId) {
    if (!window.confirm("確定要刪除這則知識嗎？")) return;
    try {
      await api("/api/admin/knowledge/delete", {
        method: "POST",
        body: JSON.stringify({ chunk_id: chunkId }),
      });
      toast("知識已刪除");
      await Promise.all([loadKnowledge(), loadStats()]);
    } catch (error) { toast(error.message, true); }
  }

  async function loadQuality() {
    const button = el("refresh-quality");
    button.disabled = true;
    try {
      const body = await api("/api/admin/knowledge/quality");
      el("stat-flagged").textContent = body.flagged;
      const labels = body.labels || {};
      el("quality-summary").innerHTML = `
        <div class="stat-card"><span>知識總數</span><strong>${body.total}</strong></div>
        <div class="stat-card"><span>結構完整</span><strong>${body.healthy}</strong></div>
        <div class="stat-card alert-stat"><span>待整理</span><strong>${body.flagged}</strong></div>
        <div class="stat-card"><span>完整度</span><strong>${body.total ? Math.round((body.healthy / body.total) * 100) : 0}%</strong></div>`;
      const counts = Object.entries(body.counts || {}).filter(([, value]) => value > 0);
      const summary = counts.map(([key, value]) => `<span class="issue-chip">${escapeHtml(labels[key] || key)} ${value}</span>`).join("");
      const samples = (body.samples || []).map((item) => {
        const custom = item.origin === "custom";
        const issues = item.issues.map((issue) => `<span class="issue-chip">${escapeHtml(labels[issue] || issue)}</span>`).join("");
        const action = custom
          ? `<button class="icon-button bordered-icon" data-edit="${escapeHtml(item.chunk_id)}" title="編輯" aria-label="編輯"><i data-lucide="pencil"></i></button>`
          : '<span class="knowledge-note">在原始檔調整</span>';
        return `<article class="knowledge-card">
          <div class="knowledge-head">
            <div><h3>${escapeHtml(item.section_title || item.title || "（無標題）")}</h3>
              <div class="knowledge-meta">${issues}<span class="source-locator">${escapeHtml(item.locator)}</span></div>
            </div>
            <div class="knowledge-actions">${action}</div>
          </div>
          <p>${escapeHtml(item.excerpt)}</p>
        </article>`;
      }).join("");
      el("quality-list").innerHTML = samples
        ? `<div class="issue-summary">${summary}</div>${samples}`
        : '<div class="empty-state">所有知識都結構完整</div>';
      window.lucide?.createIcons();
    } catch (error) {
      el("quality-list").innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
    } finally {
      button.disabled = false;
    }
  }

  async function loadUsers() {
    try {
      const body = await api("/api/admin/users");
      const rows = body.items.map((item) => `
        <tr><td><strong>${escapeHtml(item.username)}</strong></td><td><span class="role-badge ${item.role === "admin" ? "is-admin" : ""}">${item.role === "admin" ? "管理者" : "一般用戶"}</span></td><td>${item.active ? "啟用" : "停用"}</td><td>${escapeHtml(new Date(item.updated_at).toLocaleString("zh-TW"))}</td></tr>`).join("");
      el("user-results").innerHTML = rows
        ? `<table class="data-table"><thead><tr><th>帳號</th><th>權限</th><th>狀態</th><th>最後更新</th></tr></thead><tbody>${rows}</tbody></table>`
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
          role: el("user-role").value,
        }),
      });
      el("user-username").value = "";
      el("user-password").value = "";
      el("user-role").value = "user";
      toast(`帳號 ${body.user.username} 已建立或重設（${body.user.role === "admin" ? "管理者" : "一般用戶"}）`);
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
      await Promise.all([loadStats(), loadKnowledge(), loadQuality(), loadHealth()]);
    } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; }
  }

  async function tryAccountSession() {
    try {
      const response = await fetch("/api/auth/me", { cache: "no-store" });
      if (!response.ok) return false;
      const body = await response.json();
      if (body.user?.role !== "admin") return false;
      if (!(await loadStats())) return false;
      showAdmin();
      await Promise.all([loadKnowledge(), loadQuality(), loadHealth(), loadUsers()]);
      return true;
    } catch (_) {
      return false;
    }
  }

  el("knowledge-form").addEventListener("submit", loadKnowledge);
  el("knowledge-origin").addEventListener("change", () => loadKnowledge());
  el("knowledge-domain").addEventListener("change", () => loadKnowledge());
  el("new-knowledge").addEventListener("click", () => openEditor(null));
  el("editor-cancel").addEventListener("click", closeEditor);
  el("knowledge-editor").addEventListener("submit", saveKnowledge);
  document.addEventListener("click", (event) => {
    const edit = event.target.closest("[data-edit]");
    if (edit) {
      const chunk = knowledgeCache.find((item) => item.chunk_id === edit.dataset.edit);
      if (chunk) { showSection("knowledge"); openEditor(chunk); }
      else toast("請先在知識庫分頁找到這則知識", true);
      return;
    }
    const remove = event.target.closest("[data-remove]");
    if (remove) removeKnowledge(remove.dataset.remove);
  });
  el("refresh-quality").addEventListener("click", loadQuality);
  el("refresh-health").addEventListener("click", () => loadHealth(true));
  el("reindex-button").addEventListener("click", reindex);
  el("user-form").addEventListener("submit", saveUser);
  window.addEventListener("hashchange", () => {
    if (!el("admin-shell").hidden) showSection(location.hash.replace("#", ""));
  });
  window.lucide?.createIcons();
  loadProfile();
  tryAccountSession().then((entered) => {
    if (!entered) window.location.replace("/");
  });
})();
