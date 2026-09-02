class MedicineCabinetPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._panel = null;
    this._narrow = false;
    this._state = { packages: [], history: [], summary: {}, settings: {}, catalog: {} };
    this._tab = "cabinet";
    this._loading = true;
    this._error = "";
    this._scanner = null;
    this._stream = null;
    this._scanFrame = null;
    this._scanBusy = false;
    this._nativeScannerActive = false;
    this._nativeScanMessageId = null;
    this._nativeOriginalExternalBus = null;
    this._nativeExternalBusWrapper = null;
    this._form = null;
    this._instruction = null;
    this._analogs = null;
    this._prices = null;
    this._priceResults = null;
    this._priceLoading = false;
    this._priceError = "";
    this._categoryFilter = "";
    this._searchQuery = "";
    this._sortMode = "name";
    this._briefView = null;
    this._briefEdit = null;
    this._medLink = null;
    this._connected = false;
  }

  set hass(value) {
    const first = !this._hass;
    this._hass = value;
    if (first && this._connected) this._loadState();
  }

  set panel(value) {
    this._panel = value;
  }

  set narrow(value) {
    this._narrow = Boolean(value);
  }

  connectedCallback() {
    this._connected = true;
    this._render();
    if (this._hass) this._loadState();
  }

  disconnectedCallback() {
    this._connected = false;
    this._stopCamera();
  }

  async _loadState() {
    if (!this._hass) return;
    this._loading = true;
    this._error = "";
    this._render();
    try {
      this._state = await this._hass.callWS({ type: "medicine_cabinet/get_state" });
    } catch (err) {
      this._error = `Не удалось загрузить аптечку: ${err?.message || err}`;
    } finally {
      this._loading = false;
      this._render();
      if (!this._error) this._refreshLiveBriefs(true);
    }
  }

  async _refreshLiveBriefs(force = false) {
    if (!this._hass || !this._connected) return;
    try {
      const result = await this._hass.callWS({ type: "medicine_cabinet/get_live_briefs", force });
      const briefs = result?.briefs || {};
      let changed = false;
      for (const pkg of this._state.packages || []) {
        if (briefs[pkg.id]) { Object.assign(pkg, briefs[pkg.id]); changed = true; }
      }
      if (changed && this._connected) this._render();
    } catch (_) {
      // The local catalog is the offline fallback; do not block the panel if
      // RLS is unavailable or the Home Assistant host has no Internet access.
    }
  }

  _esc(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  _fmtNumber(value) {
    const num = Number(value ?? 0);
    return Number.isInteger(num) ? String(num) : num.toLocaleString("ru-RU", { maximumFractionDigits: 2 });
  }

  _fmtDate(value) {
    if (!value) return "—";
    const d = new Date(`${value}T00:00:00`);
    if (Number.isNaN(d.getTime())) return this._esc(value);
    return d.toLocaleDateString("ru-RU");
  }

  _fmtTime(value) {
    if (!value) return "";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return this._esc(value);
    return d.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", year: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  _statusText(pkg) {
    if (pkg.expired) return { cls: "danger", text: "Просрочено" };
    if (pkg.empty) return { cls: "muted", text: "Закончилось" };
    if (pkg.expiring) return { cls: "warning", text: `Срок: ${pkg.days_to_expiry} дн.` };
    if (pkg.low_stock) return { cls: "warning", text: "Заканчивается" };
    return { cls: "ok", text: "В наличии" };
  }

  _render() {
    if (!this.shadowRoot) return;
    const summary = this._state.summary || {};
    const content = this._loading
      ? `<div class="empty"><div class="spinner"></div><div>Загружаю аптечку…</div></div>`
      : this._error
        ? `<div class="empty error-box">${this._esc(this._error)}<button class="btn primary" data-action="reload">Повторить</button></div>`
        : this._renderTab();

    this.shadowRoot.innerHTML = `
      <style>${this._styles()}</style>
      <div class="app">
        <header class="topbar">
          <div class="title-wrap">
            <button class="icon-btn menu-btn" data-action="menu" aria-label="Меню">☰</button>
            <div class="brand-icon">💊</div>
            <div>
              <div class="title">Домашняя аптечка</div>
              <div class="subtitle">${summary.total || 0} упаковок · ${this._state.catalog_info?.medicines ? `${this._state.catalog_info.medicines} препаратов в локальной базе` : "локальный справочник лекарств"}</div>
            </div>
          </div>
          <div class="top-actions">
            <button class="btn ghost" data-action="reload">↻ Обновить</button>
            <button class="btn primary" data-action="scan">▣ Сканировать</button>
          </div>
        </header>

        <div class="summary-grid">
          ${this._summaryCard("Упаковок", summary.total || 0, "all", "package")}
          ${this._summaryCard("Просрочено", summary.expired || 0, "alerts", "danger")}
          ${this._summaryCard("Скоро истекает", summary.expiring || 0, "alerts", "warning")}
          ${this._summaryCard("Нужно купить", summary.shopping || 0, "shopping", "cart")}
        </div>

        <nav class="tabs">
          ${this._tabButton("cabinet", "Аптечка", "⌂")}
          ${this._tabButton("scan", "Сканировать", "▣")}
          ${this._tabButton("alerts", "Сроки", "!")}
          ${this._tabButton("shopping", "Покупки", "🛒")}
          ${this._tabButton("history", "История", "↺")}
          ${this._tabButton("settings", "Настройки", "⚙")}
        </nav>

        <main class="content">${content}</main>
        ${this._form ? this._renderModal() : ""}
        ${this._instruction ? this._renderInstructionModal() : ""}
        ${this._analogs ? this._renderAnalogsModal() : ""}
        ${this._prices ? this._renderPricesModal() : ""}
        ${this._briefView ? this._renderBriefModal() : ""}
        ${this._briefEdit ? this._renderBriefEditModal() : ""}
        ${this._medLink ? this._renderMedicationLinkModal() : ""}
        <div id="toast" class="toast"></div>
      </div>
    `;
    this._attachEvents();
  }

  _summaryCard(label, value, tab, tone) {
    return `<button class="summary-card tone-${tone}" data-tab="${tab === "all" ? "cabinet" : tab}">
      <span class="summary-value">${value}</span><span class="summary-label">${label}</span>
    </button>`;
  }

  _tabButton(id, label, icon) {
    return `<button class="tab ${this._tab === id ? "active" : ""}" data-tab="${id}"><span>${icon}</span><span>${label}</span></button>`;
  }

  _renderTab() {
    switch (this._tab) {
      case "scan": return this._renderScanner();
      case "alerts": return this._renderAlerts();
      case "shopping": return this._renderShopping();
      case "history": return this._renderHistory();
      case "settings": return this._renderSettings();
      default: return this._renderCabinet();
    }
  }

  _categories() {
    return [
      "Боль и температура", "Желудок, кишечник, печень", "Сердце и давление",
      "Простуда и дыхание", "Аллергия", "Антибиотики и инфекции", "Противовирусные",
      "Нервная система", "Суставы и мышцы", "Кожа", "Мочеполовая система",
      "Диабет", "Витамины и минералы", "Гормоны и эндокринология", "Глаза",
      "Ухо, горло, нос", "Зубы и рот", "Кровь", "Иммунная система", "Другое"
    ];
  }

  _categoryIcon(category) {
    const icons = {
      "Боль и температура":"🌡️", "Желудок, кишечник, печень":"🫃", "Сердце и давление":"❤️",
      "Простуда и дыхание":"🫁", "Аллергия":"🌿", "Антибиотики и инфекции":"🦠",
      "Противовирусные":"🛡️", "Нервная система":"🧠", "Суставы и мышцы":"🦴",
      "Кожа":"🧴", "Мочеполовая система":"💧", "Диабет":"🩸", "Витамины и минералы":"🍊",
      "Гормоны и эндокринология":"⚗️", "Глаза":"👁️", "Ухо, горло, нос":"👃",
      "Зубы и рот":"🦷", "Кровь":"🩸", "Иммунная система":"🛡️", "Другое":"💊"
    };
    return icons[category] || "💊";
  }

  _renderCabinet() {
    let packages = [...(this._state.packages || [])];
    if (!packages.length) {
      return `<div class="empty hero-empty"><div class="big-icon">💊</div><h2>Аптечка пока пустая</h2><p>Возьми упаковку лекарства и отсканируй Data Matrix или EAN камерой телефона. Название и основные данные загрузим из локального каталога.</p><button class="btn primary big" data-action="scan">▣ Сканировать первую упаковку</button><button class="btn ghost big" data-action="manual-add">＋ Добавить вручную</button></div>`;
    }

    const allPackages = [...packages];
    const presentCategories = [...new Set(allPackages.map((p) => p.category || "Другое"))]
      .sort((a,b) => a.localeCompare(b, "ru"));

    const search = (this._searchQuery || "").trim().toLocaleLowerCase("ru-RU");
    if (search) {
      packages = packages.filter((p) => [
        p.name, p.strength, p.active_ingredient, p.manufacturer, p.form,
        p.category, p.pharm_group, p.atc_code, p.atc_name, p.notes,
      ].filter(Boolean).join(" ").toLocaleLowerCase("ru-RU").includes(search));
    }
    if (this._categoryFilter) packages = packages.filter((p) => (p.category || "Другое") === this._categoryFilter);

    if (this._sortMode === "category") packages.sort((a,b) => `${a.category || "Другое"} ${a.name}`.localeCompare(`${b.category || "Другое"} ${b.name}`, "ru"));
    else if (this._sortMode === "expiry") packages.sort((a,b) => (a.expiry || "9999-99-99").localeCompare(b.expiry || "9999-99-99"));
    else if (this._sortMode === "stock") packages.sort((a,b) => Number(a.remaining || 0) - Number(b.remaining || 0));
    else packages.sort((a,b) => (a.name || "").localeCompare(b.name || "", "ru"));

    const chips = [`<button class="category-chip ${!this._categoryFilter ? "active" : ""}" data-category-filter="">Все <b>${allPackages.length}</b></button>`]
      .concat(presentCategories.map((cat) => {
        const count = allPackages.filter((p) => (p.category || "Другое") === cat).length;
        return `<button class="category-chip ${this._categoryFilter === cat ? "active" : ""}" data-category-filter="${this._esc(cat)}">${this._categoryIcon(cat)} ${this._esc(cat)} <b>${count}</b></button>`;
      })).join("");

    const context = [
      this._categoryFilter ? `категория: ${this._esc(this._categoryFilter)}` : "все категории",
      search ? `поиск: «${this._esc(this._searchQuery.trim())}»` : "",
    ].filter(Boolean).join(" · ");

    return `<div class="section-head"><div><h2>Лекарства</h2><p>${context} · ${packages.length} из ${allPackages.length} упаковок</p></div><button class="btn ghost" data-action="manual-add">＋ Добавить</button></div>
      <div class="cabinet-search-panel">
        <span class="search-icon">⌕</span>
        <input id="cabinet-search" type="search" value="${this._esc(this._searchQuery)}" placeholder="Поиск лекарства: название, действующее вещество, производитель…" autocomplete="off" spellcheck="false">
        ${this._searchQuery ? `<button class="search-clear" data-action="clear-search" title="Очистить поиск">×</button>` : ""}
      </div>
      <div class="cabinet-tools">
        <div class="category-scroll">${chips}</div>
        <label class="sort-control">Сортировка<select id="cabinet-sort">
          <option value="name" ${this._sortMode === "name" ? "selected" : ""}>По названию</option>
          <option value="category" ${this._sortMode === "category" ? "selected" : ""}>По типу лекарства</option>
          <option value="expiry" ${this._sortMode === "expiry" ? "selected" : ""}>По сроку годности</option>
          <option value="stock" ${this._sortMode === "stock" ? "selected" : ""}>По остатку</option>
        </select></label>
      </div>
      ${packages.length ? (this._sortMode === "category" && !this._categoryFilter ? this._renderCategoryGroups(packages) : `<div class="medicine-grid">${packages.map((p) => this._medicineCard(p)).join("")}</div>`) : `<div class="empty compact-empty"><div class="big-icon">🔎</div><h2>Ничего не найдено</h2><p>${search ? `По запросу «${this._esc(this._searchQuery.trim())}» лекарств нет.` : "В этой категории пока пусто."}</p>${search ? `<button class="btn ghost" data-action="clear-search">Очистить поиск</button>` : ""}</div>`}`;
  }

  _renderCategoryGroups(packages) {
    const groups = new Map();
    for (const pkg of packages) {
      const category = pkg.category || "Другое";
      if (!groups.has(category)) groups.set(category, []);
      groups.get(category).push(pkg);
    }
    return [...groups.entries()].map(([category, items]) => `
      <section class="category-group">
        <div class="category-group-head"><div><span class="category-group-icon">${this._categoryIcon(category)}</span><b>${this._esc(category)}</b></div><span>${items.length}</span></div>
        <div class="medicine-grid">${items.map((p) => this._medicineCard(p)).join("")}</div>
      </section>`).join("");
  }

  _medicineCard(pkg) {
    const st = this._statusText(pkg);
    const strength = pkg.strength ? `<span class="strength">${this._esc(pkg.strength)}</span>` : "";
    const meta = [pkg.form, pkg.active_ingredient, pkg.manufacturer].filter(Boolean).map((v) => this._esc(v)).join(" · ");
    const pct = Number(pkg.package_size) > 0 ? Math.max(0, Math.min(100, Number(pkg.remaining) / Number(pkg.package_size) * 100)) : 0;
    const category = pkg.category || "Другое";
    const instructionGtin = pkg.instruction_gtin || pkg.gtin || "";
    const rlsUrl = pkg.rls_url || pkg.source_url || "https://www.rlsnet.ru/";
    const canConsume = Number(pkg.remaining || 0) > 0;
    const disabled = (condition) => condition ? "" : " disabled aria-disabled=\"true\"";

    return `<article class="medicine-card" data-package-id="${this._esc(pkg.id)}">
      <div class="card-top">
        <span class="pill-icon">${this._categoryIcon(category)}</span>
        <span class="category-label" title="${this._esc(category)}">${this._esc(category)}</span>
        <span class="status ${st.cls}">${st.text}</span>
      </div>
      <h3>${this._esc(pkg.name)} ${strength}</h3>
      <div class="meta" title="${meta || "Данные препарата можно дополнить"}">${meta || "Данные препарата можно дополнить"}</div>
      <button class="brief-open-btn" data-action="open-brief" data-id="${this._esc(pkg.id)}">Краткая инструкция</button>
      <div class="stock-row"><div><span class="stock-num">${this._fmtNumber(pkg.remaining)}</span> <span>${this._esc(pkg.unit || "шт.")}</span></div><div class="muted-text">из ${this._fmtNumber(pkg.package_size)}</div></div>
      <div class="progress"><i style="width:${pct}%"></i></div>
      <div class="details">
        <span>📅 ${this._fmtDate(pkg.expiry)}</span>
        <span>📍 ${this._esc(pkg.location || "—")}</span>
        <span>👤 ${this._esc(pkg.owner || "Общее")}</span>
        <span class="storage-line" title="${this._esc(pkg.storage_conditions || "—")}">🌡️ ${this._esc(pkg.storage_conditions || "—")}</span>
      </div>
      <div class="card-actions">
        <div class="action-row action-row-two">
          <button class="small-btn instruction-btn" data-action="local-instruction" data-gtin="${this._esc(instructionGtin)}"${disabled(Boolean(instructionGtin && pkg.instruction_available))}>📖 Локальная инструкция</button>
          <button class="small-btn rls-btn" data-action="instruction" data-url="${this._esc(rlsUrl)}">РЛС ↗</button>
        </div>
        <div class="action-row action-row-three">
          <button class="small-btn medication-btn" data-action="medication-link" data-id="${this._esc(pkg.id)}">⏰ Добавить в приём</button>
          <button class="small-btn" data-action="consume" data-id="${this._esc(pkg.id)}"${disabled(canConsume)}>Списать</button>
          <button class="small-btn" data-action="consume-one" data-id="${this._esc(pkg.id)}"${disabled(canConsume)}>−1</button>
        </div>
        <div class="action-row action-row-three">
          <button class="small-btn prices-btn" data-action="prices" data-id="${this._esc(pkg.id)}">💰 Цены</button>
          <button class="small-btn analogs-btn" data-action="analogs" data-gtin="${this._esc(instructionGtin)}"${disabled(Boolean(instructionGtin))}>⇄ Аналоги</button>
          <button class="small-btn" data-action="edit" data-id="${this._esc(pkg.id)}">Правка</button>
        </div>
      </div>
    </article>`;
  }

  async _openMedicationLink(packageId) {
    const pkg = (this._state.packages || []).find((p) => p.id === packageId);
    if (!pkg) return this._toast("Упаковка не найдена", true);
    this._medLink = { packageId, pkg, loading: true, error: "", data: null, patient: "", medicationId: "" };
    this._render();
    try {
      const data = await this._hass.callWS({ type: "medicine_cabinet/get_medication_manager", package_id: packageId });
      const patients = data?.patients || [];
      let patient = patients.find((x) => String(x.patient).toLocaleLowerCase("ru-RU") === String(pkg.owner || "").toLocaleLowerCase("ru-RU"))?.patient || patients[0]?.patient || "";
      const selected = patients.find((x) => x.patient === patient);
      const best = (selected?.medications || []).find((m) => m.match_score > 0) || (selected?.medications || [])[0];
      this._medLink = { ...this._medLink, loading: false, data, patient, medicationId: best?.id || "" };
    } catch (err) {
      this._medLink = { ...this._medLink, loading: false, error: err?.message || String(err) };
    }
    this._render();
  }

  _renderMedicationLinkModal() {
    const m = this._medLink || {};
    const pkg = m.pkg || {};
    if (m.loading) return `<div class="modal-backdrop"><div class="modal med-link-modal"><div class="modal-head"><div><h2>Medication Manager</h2><p>${this._esc(pkg.name || "")} ${this._esc(pkg.strength || "")}</p></div><button class="icon-btn" data-action="close-medication-link">×</button></div><div class="empty"><div class="spinner"></div><div>Проверяю пациентов и расписания…</div></div></div></div>`;
    if (m.error) return `<div class="modal-backdrop"><div class="modal med-link-modal"><div class="modal-head"><div><h2>Medication Manager</h2><p>${this._esc(pkg.name || "")}</p></div><button class="icon-btn" data-action="close-medication-link">×</button></div><div class="price-warning error"><b>Связка недоступна</b><span>${this._esc(m.error)}</span></div><div class="modal-actions"><span></span><button class="btn ghost" data-action="close-medication-link">Закрыть</button></div></div></div>`;
    const data = m.data || {};
    const patients = data.patients || [];
    if (!data.available || !patients.length) return `<div class="modal-backdrop"><div class="modal med-link-modal"><div class="modal-head"><div><h2>Добавить в приём</h2><p>${this._esc(pkg.name || "")} ${this._esc(pkg.strength || "")}</p></div><button class="icon-btn" data-action="close-medication-link">×</button></div><div class="price-warning error"><b>Medication Manager не готов</b><span>Установи обновлённый backend Medication Manager 1.9.0 и перезапусти Home Assistant. После этого здесь появятся пациенты.</span></div></div></div>`;
    const patientOptions = patients.map((x) => `<option value="${this._esc(x.patient)}" ${x.patient === m.patient ? "selected" : ""}>${this._esc(x.patient)}</option>`).join("");
    const current = patients.find((x) => x.patient === m.patient) || patients[0];
    const meds = current?.medications || [];
    const medOptions = meds.map((x) => `<option value="${this._esc(x.id)}" ${x.id === m.medicationId ? "selected" : ""}>${this._esc(x.name)} ${this._esc(x.dosage || "")}${x.match_score ? ` · совпадение ${x.match_score}%` : ""}${x.cabinet_linked ? " · связано" : ""}</option>`).join("");
    const linked = meds.filter((x) => x.cabinet_linked && x.cabinet_package_id === pkg.id);
    return `<div class="modal-backdrop" data-action="close-medication-link"><div class="modal med-link-modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <div class="modal-head"><div><h2>⏰ Добавить в приём</h2><p>${this._esc(pkg.name || "")} ${this._esc(pkg.strength || "")} · остаток ${this._fmtNumber(pkg.remaining)} ${this._esc(pkg.unit || "шт.")}</p></div><button class="icon-btn" data-action="close-medication-link">×</button></div>
      ${linked.length ? `<div class="med-linked-banner"><b>✓ Уже связано с Medication Manager</b><span>${linked.map((x) => `${this._esc(current.patient)} · ${this._esc(x.name)} ${this._esc(x.dosage || "")}`).join("<br>")}</span></div>` : ""}
      <label class="med-field">Пациент<select id="med-link-patient">${patientOptions}</select></label>
      <section class="med-link-section"><h3>Связать существующее лекарство</h3><p>Расписание и история сохранятся. После «Принял» запас будет списываться из Домашней аптечки.</p>${meds.length ? `<div class="med-link-row"><select id="med-link-existing">${medOptions}</select><button class="btn ghost" data-action="link-existing-medication">Связать</button></div>` : `<div class="analog-empty">У этого пациента пока нет лекарств — создай новое ниже.</div>`}</section>
      <section class="med-link-section"><h3>Создать новое расписание</h3><div class="med-form-grid">
        <label>Время приёма<input id="med-link-times" type="text" value="08:00" placeholder="08:00, 20:00"></label>
        <label>За один приём<input id="med-link-units" type="number" min="0.01" step="0.01" value="1"></label>
        <label>Режим времени<select id="med-link-time-mode"><option value="fixed">Фиксированное</option><option value="schedule">По графику смен</option></select></label>
        <label>Интервал, дней<input id="med-link-interval" type="number" min="1" step="1" value="1"></label>
        <label>Предупреждать за, дней<input id="med-link-low-days" type="number" min="0" step="1" value="7"></label>
        <label class="med-check"><input id="med-link-permanent" type="checkbox" checked><span>Постоянный приём</span></label>
      </div><button class="btn primary" data-action="create-medication-schedule">Создать и связать</button></section>
      <div class="med-link-info"><b>Как работает остаток</b><span>Medication Manager больше не ведёт второй счётчик для связанного лекарства. После подтверждения «Принял» списывается фактический остаток здесь — сначала упаковка с ближайшим сроком годности, затем следующая.</span></div>
      ${linked.length ? `<div class="modal-actions"><span></span><button class="btn danger-btn" data-action="unlink-medication" data-medication-id="${this._esc(linked[0].id)}">Отвязать</button><button class="btn ghost" data-action="close-medication-link">Закрыть</button></div>` : `<div class="modal-actions"><span></span><button class="btn ghost" data-action="close-medication-link">Закрыть</button></div>`}
    </div></div>`;
  }

  async _createMedicationSchedule() {
    const m = this._medLink;
    if (!m) return;
    const patient = this.shadowRoot.getElementById("med-link-patient")?.value || m.patient;
    const rawTimes = this.shadowRoot.getElementById("med-link-times")?.value || "";
    const times = rawTimes.split(/[,;\s]+/).map((x) => x.trim()).filter(Boolean);
    if (!times.length || times.some((x) => !/^([01]\d|2[0-3]):[0-5]\d$/.test(x))) return this._toast("Время: HH:MM, например 08:00, 20:00", true);
    await this._hass.callWS({
      type: "medicine_cabinet/add_to_medication_manager",
      package_id: m.packageId,
      patient,
      times,
      time_mode: this.shadowRoot.getElementById("med-link-time-mode")?.value || "fixed",
      units_per_dose: Number(this.shadowRoot.getElementById("med-link-units")?.value || 1),
      interval_days: Number(this.shadowRoot.getElementById("med-link-interval")?.value || 1),
      low_supply_days: Number(this.shadowRoot.getElementById("med-link-low-days")?.value || 7),
      permanent: Boolean(this.shadowRoot.getElementById("med-link-permanent")?.checked),
      duration_days: 30,
    });
    this._toast("Лекарство добавлено в Medication Manager и связано с аптечкой");
    await this._openMedicationLink(m.packageId);
  }

  async _linkExistingMedication() {
    const m = this._medLink;
    if (!m) return;
    const patient = this.shadowRoot.getElementById("med-link-patient")?.value || m.patient;
    const medicationId = this.shadowRoot.getElementById("med-link-existing")?.value || m.medicationId;
    if (!medicationId) return this._toast("Выбери лекарство Medication Manager", true);
    await this._hass.callWS({ type: "medicine_cabinet/link_medication_manager", package_id: m.packageId, patient, medication_id: medicationId });
    this._toast("Лекарство связано. Остаток теперь берётся из Домашней аптечки");
    await this._openMedicationLink(m.packageId);
  }

  async _unlinkMedication(medicationId) {
    const m = this._medLink;
    if (!m || !medicationId) return;
    const patient = this.shadowRoot.getElementById("med-link-patient")?.value || m.patient;
    await this._hass.callWS({ type: "medicine_cabinet/unlink_medication_manager", package_id: m.packageId, patient, medication_id: medicationId });
    this._toast("Связь с аптечкой удалена");
    await this._openMedicationLink(m.packageId);
  }

  _openBriefViewer(packageId) {
    const pkg = (this._state.packages || []).find((p) => p.id === packageId);
    if (!pkg) return this._toast("Упаковка не найдена", true);
    this._briefView = packageId;
    this._render();
  }

  _renderBriefModal() {
    const pkg = (this._state.packages || []).find((p) => p.id === this._briefView);
    if (!pkg) return "";
    const source = pkg.brief_custom ? "Моя инструкция" : (pkg.brief_source || (pkg.brief_live ? "RLSnet.ru" : "Локальная база"));
    const rows = [
      ["Для чего", pkg.brief_indications],
      ["Как принимать", pkg.brief_dosage],
      ["Важно", pkg.brief_contraindications],
    ].filter(([, value]) => Boolean(value));
    const body = rows.length
      ? `<div class="brief-view-sections">${rows.map(([title, value]) => `<section><h3>${title}</h3><div>${this._esc(value)}</div></section>`).join("")}</div>`
      : `<div class="brief-view-empty">Краткая инструкция пока не заполнена. Нажми «Обновить» или «Редактировать».</div>`;
    return `<div class="modal-backdrop" data-action="close-brief"><div class="modal brief-view-modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <div class="modal-head"><div><h2>Краткая инструкция</h2><p>${this._esc(pkg.name || "")} ${this._esc(pkg.strength || "")} · ${this._esc(source)}</p></div><button class="icon-btn" data-action="close-brief">×</button></div>
      ${body}
      <div class="brief-view-actions">
        <button class="btn ghost" data-action="refresh-briefs">↻ Обновить</button>
        <button class="btn primary" data-action="edit-brief" data-id="${this._esc(pkg.id)}">✎ Редактировать</button>
        <button class="btn ghost" data-action="close-brief">Закрыть</button>
      </div>
    </div></div>`;
  }

  _openBriefEditor(packageId) {
    const pkg = (this._state.packages || []).find((p) => p.id === packageId);
    if (!pkg) return this._toast("Упаковка не найдена", true);
    const custom = Boolean(pkg.brief_custom);
    this._briefView = null;
    this._briefEdit = {
      package_id: pkg.id,
      name: pkg.name || "Лекарство",
      strength: pkg.strength || "",
      was_custom: custom,
      indications: custom ? (pkg.brief_custom_indications || "") : (pkg.brief_indications || ""),
      dosage: custom ? (pkg.brief_custom_dosage || "") : (pkg.brief_dosage || ""),
      contraindications: custom ? (pkg.brief_custom_contraindications || "") : (pkg.brief_contraindications || ""),
    };
    this._render();
  }

  _renderBriefEditModal() {
    const b = this._briefEdit || {};
    return `<div class="modal-backdrop" data-action="close-brief-editor"><div class="modal brief-edit-modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <div class="modal-head"><div><h2>Краткая инструкция</h2><p>${this._esc(b.name || "")} ${this._esc(b.strength || "")}</p></div><button class="icon-btn" data-action="close-brief-editor">×</button></div>
      <div class="brief-edit-note"><b>Своя краткая версия</b><span>После сохранения этот текст имеет приоритет над локальной базой и RLSnet.ru. Полную инструкцию всё равно проверяй в официальном источнике.</span></div>
      <div class="brief-edit-fields">
        <label>Для чего<textarea id="brief-edit-indications" rows="4" placeholder="Кратко: основные показания…">${this._esc(b.indications || "")}</textarea></label>
        <label>Как принимать<textarea id="brief-edit-dosage" rows="4" placeholder="Кратко: способ применения и дозы…">${this._esc(b.dosage || "")}</textarea></label>
        <label>Важно<textarea id="brief-edit-contraindications" rows="4" placeholder="Кратко: основные противопоказания / важное предупреждение…">${this._esc(b.contraindications || "")}</textarea></label>
      </div>
      <div class="modal-actions">
        ${b.was_custom ? `<button class="btn ghost" data-action="reset-brief">↻ Вернуть из RLS</button>` : ""}
        <span></span><button class="btn ghost" data-action="close-brief-editor">Отмена</button><button class="btn primary" data-action="save-brief">Сохранить</button>
      </div>
    </div></div>`;
  }

  async _saveBrief() {
    const b = this._briefEdit;
    if (!b?.package_id) return;
    const indications = this.shadowRoot.getElementById("brief-edit-indications")?.value?.trim() || "";
    const dosage = this.shadowRoot.getElementById("brief-edit-dosage")?.value?.trim() || "";
    const contraindications = this.shadowRoot.getElementById("brief-edit-contraindications")?.value?.trim() || "";
    await this._hass.callWS({
      type: "medicine_cabinet/update_package",
      package_id: b.package_id,
      package: {
        brief_custom: true,
        brief_custom_indications: indications,
        brief_custom_dosage: dosage,
        brief_custom_contraindications: contraindications,
      },
    });
    this._briefEdit = null;
    await this._loadState();
    this._toast("Краткая инструкция сохранена");
  }

  async _resetBrief() {
    const b = this._briefEdit;
    if (!b?.package_id) return;
    await this._hass.callWS({
      type: "medicine_cabinet/update_package",
      package_id: b.package_id,
      package: {
        brief_custom: false,
        brief_custom_indications: "",
        brief_custom_dosage: "",
        brief_custom_contraindications: "",
      },
    });
    this._briefEdit = null;
    await this._loadState();
    this._toast("Ручная версия удалена — снова используется RLS / локальная база");
  }

  _renderScanner() {
    return `<div class="scanner-layout">
      <section class="scanner-card">
        <div class="section-head"><div><h2>Сканирование упаковки</h2><p>Наведи заднюю камеру прежде всего на квадратный Data Matrix. Линейный EAN используется как резерв.</p></div></div>
        <div class="video-wrap"><video id="scanner-video" playsinline muted></video><div class="scan-frame"><i></i><i></i><i></i><i></i></div><div id="camera-placeholder" class="camera-placeholder">📷<br><span>Камера ещё не запущена</span></div></div>
        <div id="scan-status" class="scan-status">На Android используется встроенный сканер Home Assistant; данные препарата ищутся только в локальном каталоге.</div>
        <div class="scanner-actions"><button class="btn primary big" data-action="start-camera">Запустить сканер</button><button class="btn ghost big" data-action="stop-camera">Остановить</button></div>
      </section>
      <section class="manual-card"><h3>Резервный ввод</h3><p>Если камера недоступна, вставь полный Data Matrix или штрихкод вручную.</p><textarea id="manual-code" placeholder="01… / ]d2… / EAN-13"></textarea><button class="btn ghost" data-action="parse-manual">Распознать код</button><div class="secure-note">Android Companion App использует встроенный нативный сканер Home Assistant. В обычном браузере используется Web API камеры, которому может потребоваться HTTPS.</div></section>
    </div>`;
  }

  _renderAlerts() {
    const items = (this._state.packages || []).filter((p) => p.expired || p.expiring);
    if (!items.length) return `<div class="empty"><div class="big-icon">✅</div><h2>По срокам всё хорошо</h2><p>Нет просроченных упаковок и препаратов, срок которых подходит к концу.</p></div>`;
    return `<div class="section-head"><div><h2>Сроки годности</h2><p>Предупреждение сейчас: за ${this._state.settings?.expiry_warning_days ?? 30} дней.</p></div></div><div class="medicine-grid">${items.map((p) => this._medicineCard(p)).join("")}</div>`;
  }

  _renderShopping() {
    const items = (this._state.packages || []).filter((p) => p.shopping);
    if (!items.length) return `<div class="empty"><div class="big-icon">🛒</div><h2>Покупать ничего не нужно</h2><p>Остатки выше установленных минимальных значений.</p></div>`;
    return `<div class="section-head"><div><h2>Нужно купить</h2><p>Сюда автоматически попадают закончившиеся и заканчивающиеся препараты.</p></div></div><div class="shopping-list">${items.map((p) => `<div class="shopping-item"><div><b>${this._esc(p.name)} ${this._esc(p.strength || "")}</b><span>Осталось ${this._fmtNumber(p.remaining)} ${this._esc(p.unit)} · минимум ${this._fmtNumber(p.low_stock_threshold)}</span></div><button class="btn ghost" data-action="edit" data-id="${this._esc(p.id)}">Открыть</button></div>`).join("")}</div>`;
  }

  _renderHistory() {
    const history = this._state.history || [];
    if (!history.length) return `<div class="empty"><div class="big-icon">↺</div><h2>История пока пустая</h2><p>Добавления, списания и корректировки будут сохраняться здесь.</p></div>`;
    return `<div class="section-head"><div><h2>История движения</h2><p>Последние операции с домашней аптечкой.</p></div></div><div class="history-list">${history.map((h) => {
      const delta = h.delta === null || h.delta === undefined ? "" : `${Number(h.delta) > 0 ? "+" : ""}${this._fmtNumber(h.delta)} ${this._esc(h.unit)}`;
      return `<div class="history-row"><div class="history-dot"></div><div class="history-main"><b>${this._esc(h.name)} ${this._esc(h.strength || "")}</b><span>${this._esc(h.note || h.action)}</span></div><div class="history-side"><b>${delta}</b><span>${this._fmtTime(h.timestamp)}</span></div></div>`;
    }).join("")}</div>`;
  }

  _renderSettings() {
    const s = this._state.settings || {};
    const ci = this._state.catalog_info || {};
    const catalogStatus = ci.installed ? "✅ установлен" : "⚠️ не найден";
    const stat = (label, value, note = "") => `<div class="catalog-stat"><b>${this._fmtNumber(value || 0)}</b><span>${label}</span>${note ? `<small>${this._esc(note)}</small>` : ""}</div>`;
    return `<div class="settings-grid">
      <section class="settings-card"><h2>Срок годности и цены</h2><label>Предупреждать за, дней<input id="expiry-warning" type="number" min="1" max="365" value="${Number(s.expiry_warning_days ?? 30)}"></label><label>Город для цен<input id="price-city" type="text" value="${this._esc(s.price_city || "Москва")}" placeholder="Москва"></label><label class="switch-row"><span><b>Ежедневное уведомление Home Assistant</b><small>Создаётся одно уведомление, если есть просроченные, заканчивающиеся или заканчивающиеся по сроку препараты.</small></span><input id="notifications-enabled" type="checkbox" ${s.notifications_enabled !== false ? "checked" : ""}></label><button class="btn primary" data-action="save-settings">Сохранить</button></section>
      <section class="settings-card catalog-card"><h2>Локальный каталог</h2><p><b>Состояние:</b> ${catalogStatus}</p>${ci.installed ? `<div class="catalog-stats">
        ${stat("Препаратов", ci.medicines, "торговых наименований")}
        ${stat("Лекарственных позиций", ci.reference_drugs || ci.medicine_rows, "индекс для аналогов")}
        ${stat("Упаковок с кодом", ci.medicine_rows)}
        ${stat("Штрихкодов", ci.barcodes)}
        ${stat("Описаний", ci.descriptions)}
        ${stat("Инструкций", ci.instructions)}
        ${stat("Текстовых записей всего", ci.text_records)}
      </div><p class="catalog-meta">С текстом: ${this._fmtNumber(ci.medicine_rows_with_text || 0)} позиций${ci.generated_at ? ` · база создана ${this._fmtTime(ci.generated_at)}` : ""}</p>` : ""}<p><code>/config/medicine_cabinet/medicine_catalog.sqlite</code></p></section>
      <section class="settings-card"><h2>Места хранения</h2><p>Эти варианты доступны во всплывающем списке при добавлении и правке упаковки.</p><div class="location-settings-list">${this._storageLocations().map((name) => `<span>📍 ${this._esc(name)}</span>`).join("")}</div><button class="btn ghost" data-action="add-storage-location">＋ Добавить место</button></section>
      <section class="settings-card"><h2>Интеграция</h2><p><b>Версия:</b> ${this._esc(this._panel?.config?.version || "0.4.4")}</p><p><b>Домашние остатки:</b> локальное хранилище <code>.storage</code></p><p><b>Справочник:</b> ${catalogStatus}</p><p><b>Сканер:</b> нативный сканер Home Assistant на Android + браузерный Barcode Detection API</p><p><b>Интернет:</b> карточки автоматически получают краткие разделы инструкции с официального RLSnet.ru; автоответы кэшируются только в памяти. Свою краткую инструкцию можно сохранить вручную — она хранится вместе с упаковкой и не перезаписывается RLS. Полная инструкция открывается на RLSnet.ru.</p><p><b>Цены:</b> при открытии окна приложение сначала читает публичную таблицу «Заказ в аптеках» на RLSnet.ru для точной упаковки, затем при необходимости пробует Горздрав, Планету здоровья, Столички и Фармленд напрямую. Есть ручная кнопка обновления; при блокировке автоматического запроса остаётся переход на официальный сайт.</p><p><b>Аналоги:</b> локально считаются по действующему веществу и АТХ. Это справочная выборка, не рекомендация по замене.</p><p><b>Medication Manager:</b> v0.4 поддерживает создание расписания прямо из карточки и привязку существующего лекарства. После «Принял» остаток списывается из аптечки по ближайшему сроку годности.</p><p><b>Автоматизации:</b> доступны действия <code>medicine_cabinet.consume</code>, <code>medicine_cabinet.adjust</code> и внутреннее <code>consume_linked</code>.</p></section>
    </div>`;
  }

  _blankForm(parsed = {}) {
    const cat = parsed.catalog || {};
    const packageSize = cat.package_size || "";
    return {
      id: null,
      gtin: parsed.gtin || cat.gtin || "",
      raw_code: parsed.raw_code || "",
      expiry_source: parsed.expiry_source || "",
      production_date: parsed.production_date || "",
      best_before: parsed.best_before || "",
      barcode_format: parsed.barcode_format || "",
      serial: parsed.serial || "",
      lot: parsed.lot || "",
      name: cat.name || "",
      strength: cat.strength || "",
      form: cat.form || "",
      manufacturer: cat.manufacturer || "",
      active_ingredient: cat.active_ingredient || "",
      atc_code: cat.atc_code || "",
      atc_name: cat.atc_name || "",
      pharm_group: cat.pharm_group || "",
      packing_name: cat.packing_name || "",
      shelf_life: cat.shelf_life || "",
      shelf_life_months: cat.shelf_life_months || 0,
      storage_conditions: cat.storage_conditions || "",
      prescription: cat.prescription || "",
      instruction_available: Boolean(cat.instruction_available),
      category: cat.category || "Другое",
      source: cat.source || "",
      source_url: cat.source_url || cat.rls_url || "",
      package_size: packageSize,
      remaining: packageSize,
      unit: cat.unit || "шт.",
      low_stock_threshold: "",
      expiry: parsed.expiry || "",
      owner: "Общее",
      location: "Основная аптечка",
      instruction_url: cat.instruction_url || cat.rls_url || "",
      notes: "",
    };
  }

  _renderModal() {
    const f = this._form;
    return `<div class="modal-backdrop" data-action="close-modal"><div class="modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <div class="modal-head"><div><h2>${f.id ? "Упаковка лекарства" : "Новая упаковка"}</h2><p>${f.gtin ? `GTIN: ${this._esc(f.gtin)}` : "Заполни данные препарата"}</p></div><button class="icon-btn" data-action="close-modal">×</button></div>
      <div class="form-grid">
        ${this._field("Название", "name", f.name, "text", true)}
        ${this._field("Дозировка / концентрация", "strength", f.strength)}
        ${this._formField(f.form || "")}
        ${this._field("Производитель", "manufacturer", f.manufacturer)}
        ${this._field("Действующее вещество", "active_ingredient", f.active_ingredient || "")}
        ${this._field("Код АТХ", "atc_code", f.atc_code || "")}
        ${this._categoryField(f.category || "Другое")}
        ${this._numberField("Количество в упаковке", "package_size", f.package_size)}
        ${this._numberField("Текущий остаток", "remaining", f.remaining)}
        ${this._field("Единица", "unit", f.unit)}
        ${this._numberField("Минимальный остаток", "low_stock_threshold", f.low_stock_threshold)}
        <label>Срок годности<input name="expiry" type="date" value="${this._esc(f.expiry)}"><small class="field-note ${f.expiry_source === "datamatrix_ai17" ? "ok" : ""}">${f.expiry_source === "datamatrix_ai17" ? "✓ Считан из Data Matrix · AI (17)" : `В Data Matrix этой упаковки срок не найден${f.shelf_life ? ` · справочно: ${this._esc(f.shelf_life)}` : ""}. Введи дату, напечатанную на коробке.`}</small></label>
        ${this._storageLocationField(f.location)}
        ${this._patientField(f.owner || "Общее")}
        ${this._field("Серия", "lot", f.lot)}
        <label>Серийный номер<input name="serial" type="text" value="${this._esc(f.serial)}"><small class="field-note">Одинаковый серийный номер одной упаковки повторно сохранить нельзя.</small></label>
        ${this._field("GTIN", "gtin", f.gtin)}
        <label class="full">Внешняя ссылка на инструкцию (необязательно)<input name="instruction_url" type="url" value="${this._esc(f.instruction_url)}" placeholder="https://…"></label>
        <label class="full">Заметка<textarea name="notes" placeholder="Например: после вскрытия хранить…">${this._esc(f.notes)}</textarea></label>
      </div>
      ${f.source ? `<div class="lookup-source">Данные: <b>${this._esc(f.source)}</b></div>` : ""}
      <div class="modal-actions">${f.id ? `<button class="btn danger-btn" data-action="delete-package" data-id="${this._esc(f.id)}">Удалить</button>` : ""}<button class="btn ghost" data-action="lookup-form" ${f.gtin ? "" : "disabled"}>↻ Подгрузить данные</button><span></span><button class="btn ghost" data-action="close-modal">Отмена</button><button class="btn primary" data-action="save-package">Сохранить</button></div>
    </div></div>`;
  }

  _renderInstructionModal() {
    const i = this._instruction || {};
    const sections = [
      ["Состав", "composition"],
      ["Описание лекарственной формы", "dosage_form_description"],
      ["Фармакодинамика", "pharmacodynamics"],
      ["Фармакокинетика", "pharmacokinetics"],
      ["Показания", "indications"],
      ["Противопоказания", "contraindications"],
      ["Способ применения и дозы", "dosage"],
      ["Побочные действия", "side_effects"],
      ["Взаимодействия", "interactions"],
      ["Передозировка", "overdose"],
      ["Особые указания", "special_instructions"],
      ["Форма выпуска / упаковка", "package_info"],
      ["Беременность и грудное вскармливание", "pregnancy"],
      ["Производитель", "manufacturer_info"],
      ["Условия отпуска", "dispensing_conditions"],
    ].filter(([, key]) => String(i[key] || "").trim());
    return `<div class="modal-backdrop" data-action="close-instruction"><div class="modal instruction-modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <div class="modal-head"><div><h2>${this._esc(i.name || "Инструкция")}${i.strength ? ` ${this._esc(i.strength)}` : ""}</h2><p>${this._esc(i.active_ingredient || "")} · локальная база</p></div><button class="icon-btn" data-action="close-instruction">×</button></div>
      ${sections.length ? `<div class="instruction-sections">${sections.map(([title,key]) => `<section><h3>${title}</h3><div>${this._esc(i[key])}</div></section>`).join("")}</div>` : `<div class="external-info"><b>Локальной инструкции для этой позиции нет.</b><span>Полную актуальную информацию можно открыть на официальном сайте РЛС.</span></div>`}
      <div class="instruction-footer">
        <span>Локальная база может быть неполной или старее сайта.</span>
        <div>${i.rls_analogs_url ? `<button class="btn ghost" data-action="instruction" data-url="${this._esc(i.rls_analogs_url)}">⇄ Аналоги на РЛС ↗</button>` : ""}${i.rls_url ? `<button class="btn primary" data-action="instruction" data-url="${this._esc(i.rls_url)}">Открыть на РЛС ↗</button>` : ""}</div>
      </div>
    </div></div>`;
  }

  async _openInstruction(gtin) {
    if (!gtin) return this._toast("У упаковки нет GTIN", true);
    this._toast("Открываю локальную инструкцию…");
    const result = await this._hass.callWS({ type: "medicine_cabinet/get_instruction", gtin });
    if (!result.instruction) return this._toast(result.error || "Препарат в локальном каталоге не найден", true);
    this._instruction = result.instruction;
    this._render();
  }

  _renderAnalogsModal() {
    const a = this._analogs || {};
    const renderItems = (items) => {
      if (!items?.length) return `<div class="analog-empty">В локальном каталоге совпадений не найдено.</div>`;
      return `<div class="analog-list">${items.map((item) => {
        const match = [item.same_strength ? "та же дозировка" : "", item.same_form ? "та же форма" : ""].filter(Boolean);
        return `<article class="analog-item">
          <div class="analog-main"><b>${this._esc(item.name)} ${item.strength ? `<span>${this._esc(item.strength)}</span>` : ""}</b>
          <small>${this._esc([item.form, item.manufacturer].filter(Boolean).join(" · ") || "—")}</small>
          <small>${this._esc(item.active_ingredient || "")}${item.atc_code ? ` · ATХ ${this._esc(item.atc_code)}` : ""}</small>
          ${match.length ? `<div class="match-chips">${match.map((m) => `<em>${this._esc(m)}</em>`).join("")}</div>` : ""}</div>
          ${item.rls_specific_url ? `<button class="small-btn rls-btn" data-action="instruction" data-url="${this._esc(item.rls_url)}">РЛС ↗</button>` : ""}
        </article>`;
      }).join("")}</div>`;
    };
    return `<div class="modal-backdrop" data-action="close-analogs"><div class="modal analogs-modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <div class="modal-head"><div><h2>Аналоги: ${this._esc(a.name || "Препарат")} ${this._esc(a.strength || "")}</h2><p>${this._esc(a.active_ingredient || "")}${a.atc_code ? ` · ATХ ${this._esc(a.atc_code)}` : ""}</p></div><button class="icon-btn" data-action="close-analogs">×</button></div>
      <div class="analog-warning"><b>Важно:</b> это справочная выборка, а не рекомендация по замене. У препаратов могут отличаться дозировка, лекарственная форма, показания, противопоказания и режим отпуска.</div>
      <section class="analog-section"><div class="analog-section-head"><div><h3>По действующему веществу</h3><p>Наиболее близкие варианты из локальной базы; выше ставятся совпадения по дозировке и форме.</p></div><b>${(a.by_active_ingredient || []).length}</b></div>${renderItems(a.by_active_ingredient || [])}</section>
      <section class="analog-section"><div class="analog-section-head"><div><h3>По АТХ</h3><p>Препараты той же АТХ-группы, но действующее вещество может отличаться.</p></div><b>${(a.by_atc || []).length}</b></div>${renderItems(a.by_atc || [])}</section>
      <div class="instruction-footer"><span>Полный и актуальный список проверяй на официальном сайте РЛС.</span><div>${a.rls_analogs_url ? `<button class="btn primary" data-action="instruction" data-url="${this._esc(a.rls_analogs_url)}">Все аналоги на РЛС ↗</button>` : ""}</div></div>
    </div></div>`;
  }

  async _openAnalogs(gtin) {
    if (!gtin) return this._toast("У упаковки нет GTIN", true);
    this._toast("Ищу аналоги в локальном каталоге…");
    const result = await this._hass.callWS({ type: "medicine_cabinet/get_analogs", gtin });
    if (!result.analogs) return this._toast(result.error || "Аналоги не найдены", true);
    this._analogs = result.analogs;
    this._render();
  }

  _priceQuery(pkg) {
    const size = Number(pkg?.package_size || 0) > 0 ? this._fmtNumber(pkg.package_size) : "";
    const unit = size ? (pkg?.unit || "шт") : "";
    return [pkg?.name, pkg?.strength, size, unit].filter(Boolean).join(" ").replace(/\s+/g, " ").trim();
  }

  _renderPricesModal() {
    const pkg = this._prices || {};
    const query = this._priceQuery(pkg);
    const priceData = this._priceResults || {};
    const rows = priceData.results || [];
    const fallback = [
      { key:"gorzdrav", name:"Горздрав", icon:"🏥", url:`https://gorzdrav.org/search/?text=${encodeURIComponent(query)}` },
      { key:"planeta", name:"Планета здоровья", icon:"🪐", url:`https://planetazdorovo.ru/search/?q=${encodeURIComponent(query)}` },
      { key:"stolichki", name:"Столички", icon:"💊", url:`https://stolichki.ru/search?query=${encodeURIComponent(query)}` },
      { key:"farmlend", name:"Фармленд", icon:"🌿", url:`https://farmlend.ru/search/?q=${encodeURIComponent(query)}` },
    ];
    const networks = rows.length ? rows : fallback;
    const cards = networks.map((n) => {
      const loading = this._priceLoading && !rows.length;
      const price = n.price || "";
      const availability = n.availability || "";
      const ok = Boolean(price || availability);
      const status = loading ? `<span class="price-loading"><i></i> Получаю…</span>`
        : ok ? `<div class="price-value">${this._esc(price || "Цена на сайте")}</div><div class="price-stock">${this._esc(availability || "наличие уточняется")}${n.via ? ` · ${this._esc(n.via)}` : ""}</div>`
        : `<div class="price-missing">${this._esc(n.error || "Автоматически получить цену не удалось")}</div>`;
      return `<article class="pharmacy-item ${ok ? "has-price" : ""}"><div class="pharmacy-icon">${n.icon || "💊"}</div><div class="pharmacy-main"><b>${this._esc(n.name)}</b>${status}</div><button class="btn ${ok ? "primary" : "ghost"}" data-action="pharmacy" data-url="${this._esc(n.url || "")}" data-query="${this._esc(query)}" data-copy="0">Открыть ↗</button></article>`;
    }).join("");
    const city = priceData.city || this._state.settings?.price_city || "Москва";
    return `<div class="modal-backdrop" data-action="close-prices"><div class="modal prices-modal" role="dialog" aria-modal="true" onclick="event.stopPropagation()">
      <div class="modal-head"><div><h2>Цены и наличие</h2><p>${this._esc(query || pkg.name || "Лекарство")} · ${this._esc(city)}</p></div><button class="icon-btn" data-action="close-prices">×</button></div>
      <div class="price-toolbar"><div><b>Актуальные цены</b><span>${this._priceLoading ? "Получаю цены с RLS и сайтов аптек…" : (priceData.updated_at ? `Обновлено ${new Date(priceData.updated_at * 1000).toLocaleTimeString("ru-RU", {hour:"2-digit",minute:"2-digit"})}` : "Данные ещё не получены")}</span></div><button class="btn primary" data-action="refresh-prices" ${this._priceLoading ? "disabled" : ""}>↻ ${this._priceLoading ? "Получаю" : "Обновить / получить"}</button></div>
      ${this._priceError ? `<div class="price-warning error"><b>Не все сайты ответили.</b><span>${this._esc(this._priceError)}</span></div>` : `<div class="price-warning"><b>Цена и наличие меняются в реальном времени.</b><span>Сначала проверяется таблица «Заказ в аптеках» на RLSnet.ru, затем — сайт самой сети. Если автоматический запрос блокируется, кнопка «Открыть» ведёт на официальный сайт.</span></div>`}
      <div class="pharmacy-list">${cards}</div>
      <div class="instruction-footer"><span>Город поиска меняется в Настройках. Аптечные сайты могут дополнительно учитывать выбранную у них аптеку или геолокацию.</span><div><button class="btn ghost" data-action="close-prices">Закрыть</button></div></div>
    </div></div>`;
  }

  async _openPrices(packageId) {
    const pkg = (this._state.packages || []).find((item) => item.id === packageId);
    if (!pkg) return this._toast("Упаковка не найдена", true);
    this._prices = pkg;
    this._priceResults = null;
    this._priceError = "";
    this._render();
    await this._refreshPrices(false);
  }

  async _refreshPrices(force = true) {
    if (!this._prices || !this._hass) return;
    this._priceLoading = true;
    this._priceError = "";
    this._render();
    try {
      this._priceResults = await this._hass.callWS({ type:"medicine_cabinet/get_prices", package_id:this._prices.id, force });
      const failed = (this._priceResults?.results || []).filter((x) => !x.price && !x.availability);
      if (failed.length === 4) this._priceError = "Аптечные сайты не отдали цены автоматически. Можно открыть их по кнопкам ниже.";
      else if (failed.length) this._priceError = `Не удалось автоматически получить данные: ${failed.map((x) => x.name).join(", ")}.`;
    } catch (err) {
      this._priceError = err?.message || String(err);
    } finally {
      this._priceLoading = false;
      if (this._prices) this._render();
    }
  }

  _categoryField(value) {
    const options = this._categories().map((cat) => `<option value="${this._esc(cat)}" ${cat === value ? "selected" : ""}>${this._esc(cat)}</option>`).join("");
    return `<label>Тип лекарства<select name="category">${options}</select></label>`;
  }

  _numberField(label, name, value) {
    return `<label>${label}<input name="${name}" type="number" value="${this._esc(value)}" step="0.5" min="0" inputmode="decimal"></label>`;
  }

  _storageLocations() {
    const defaults = ["Основная аптечка", "Кухня", "Комод", "Шкаф Таня"];
    const saved = Array.isArray(this._state.settings?.storage_locations) ? this._state.settings.storage_locations : [];
    const used = (this._state.packages || []).map((p) => String(p.location || "").trim()).filter(Boolean);
    const out = [];
    const seen = new Set();
    for (const value of [...defaults, ...saved, ...used]) {
      const name = String(value || "").replace(/\s+/g, " ").trim();
      const key = name.toLocaleLowerCase("ru-RU");
      if (!name || seen.has(key)) continue;
      seen.add(key);
      out.push(name);
    }
    return out;
  }

  _storageLocationField(value) {
    const current = String(value || "Основная аптечка").trim() || "Основная аптечка";
    const values = this._storageLocations();
    if (!values.some((x) => x.toLocaleLowerCase("ru-RU") === current.toLocaleLowerCase("ru-RU"))) values.push(current);
    const options = values.map((name) => `<option value="${this._esc(name)}" ${name === current ? "selected" : ""}>${this._esc(name)}</option>`).join("");
    return `<label>Место хранения<select id="package-location" name="location">${options}<option value="__add__">＋ Добавить другое место…</option></select><small class="field-note">Новое место сохранится в списке для следующих упаковок.</small></label>`;
  }

  async _addStorageLocation(fromForm = false) {
    const previous = String(this._form?.location || "Основная аптечка");
    const draft = fromForm && this._form ? this._collectForm() : null;
    const raw = window.prompt("Название нового места хранения", "");
    if (raw === null) {
      if (draft) { draft.location = previous; this._form = draft; this._render(); }
      return;
    }
    const name = String(raw).replace(/\s+/g, " ").trim();
    if (!name) {
      if (draft) { draft.location = previous; this._form = draft; this._render(); }
      return this._toast("Название места не может быть пустым", true);
    }
    if (name.length > 80) {
      if (draft) { draft.location = previous; this._form = draft; this._render(); }
      return this._toast("Название места слишком длинное", true);
    }

    const locations = this._storageLocations();
    const exists = locations.find((x) => x.toLocaleLowerCase("ru-RU") === name.toLocaleLowerCase("ru-RU"));
    const finalName = exists || name;
    if (!exists) locations.push(name);

    try {
      const settings = await this._hass.callWS({
        type: "medicine_cabinet/update_settings",
        settings: { storage_locations: locations },
      });
      this._state.settings = { ...(this._state.settings || {}), ...(settings || {}) };
      if (draft) { draft.location = finalName; this._form = draft; }
      this._render();
      this._toast(exists ? `Место «${finalName}» уже есть в списке` : `Добавлено место: ${finalName}`);
    } catch (err) {
      if (draft) { draft.location = previous; this._form = draft; this._render(); }
      this._toast(err?.message || String(err), true);
    }
  }

  _patientField(value) {
    const current = String(value || "Общее").trim() || "Общее";
    const patients = Array.from(new Set([
      ...(this._state.patients || []).map((x) => String(x || "").trim()).filter(Boolean),
      ...(current && current !== "Общее" ? [current] : []),
    ])).sort((a, b) => a.localeCompare(b, "ru"));
    const options = ["Общее", ...patients.filter((x) => x !== "Общее")].map((name) =>
      `<option value="${this._esc(name)}" ${name === current ? "selected" : ""}>${this._esc(name)}</option>`
    ).join("");
    return `<label>Пациент<select name="owner">${options}</select><small class="field-note">Общее — упаковка доступна всем пациентам.</small></label>`;
  }

  _formField(value) {
    const current = String(value || "").trim();
    const common = [
      "Таблетки", "Капсулы", "Раствор", "Сироп", "Суспензия", "Капли",
      "Спрей", "Аэрозоль / ингалятор", "Порошок", "Гранулы", "Мазь",
      "Крем", "Гель", "Свечи / суппозитории", "Пластырь", "Ампулы / инъекции",
      "Драже", "Пастилки / таблетки для рассасывания", "Другое"
    ];
    const values = current && !common.includes(current) ? [current, ...common] : common;
    const options = [`<option value="" ${!current ? "selected" : ""}>Не выбрано</option>`, ...values.map((name) =>
      `<option value="${this._esc(name)}" ${name === current ? "selected" : ""}>${this._esc(name)}</option>`
    )].join("");
    return `<label>Форма<select name="form">${options}</select></label>`;
  }

  _field(label, name, value, type = "text", required = false, placeholder = "") {
    const step = type === "number" ? ' step="0.5" min="0"' : "";
    return `<label>${label}<input name="${name}" type="${type}" value="${this._esc(value)}" ${required ? "required" : ""}${step} placeholder="${this._esc(placeholder)}"></label>`;
  }

  _attachEvents() {
    this.shadowRoot.querySelectorAll("[data-tab]").forEach((el) => el.addEventListener("click", (ev) => {
      this._stopCamera();
      this._tab = ev.currentTarget.dataset.tab;
      this._render();
    }));

    this.shadowRoot.querySelectorAll("[data-action]").forEach((el) => el.addEventListener("click", (ev) => this._handleAction(ev)));
    this.shadowRoot.querySelectorAll("[data-category-filter]").forEach((el) => el.addEventListener("click", (ev) => {
      this._categoryFilter = ev.currentTarget.dataset.categoryFilter || "";
      this._render();
    }));
    this.shadowRoot.getElementById("cabinet-sort")?.addEventListener("change", (ev) => {
      this._sortMode = ev.currentTarget.value || "name";
      this._render();
    });
    this.shadowRoot.getElementById("package-location")?.addEventListener("change", (ev) => {
      if (ev.currentTarget.value === "__add__") this._addStorageLocation(true);
    });
    this.shadowRoot.getElementById("med-link-patient")?.addEventListener("change", (ev) => {
      if (!this._medLink) return;
      this._medLink.patient = ev.currentTarget.value || "";
      const patient = (this._medLink.data?.patients || []).find((x) => x.patient === this._medLink.patient);
      const best = (patient?.medications || []).find((x) => x.match_score > 0) || (patient?.medications || [])[0];
      this._medLink.medicationId = best?.id || "";
      this._render();
    });
    this.shadowRoot.getElementById("med-link-existing")?.addEventListener("change", (ev) => {
      if (this._medLink) this._medLink.medicationId = ev.currentTarget.value || "";
    });
    this.shadowRoot.getElementById("cabinet-search")?.addEventListener("input", (ev) => {
      this._searchQuery = ev.currentTarget.value || "";
      clearTimeout(this._searchTimer);
      this._searchTimer = setTimeout(() => {
        if (!this._connected || this._tab !== "cabinet") return;
        this._render();
        const input = this.shadowRoot.getElementById("cabinet-search");
        if (input) {
          input.focus({ preventScroll: true });
          const pos = input.value.length;
          try { input.setSelectionRange(pos, pos); } catch (_) {}
        }
      }, 90);
    });
  }

  async _handleAction(ev) {
    const el = ev.currentTarget;
    const action = el.dataset.action;
    if (el.disabled || el.getAttribute("aria-disabled") === "true") return;
    try {
      if (action === "menu") this.dispatchEvent(new Event("hass-toggle-menu", { bubbles: true, composed: true }));
      if (action === "reload") await this._loadState();
      if (action === "scan") { this._tab = "scan"; this._render(); setTimeout(() => this._startCamera(), 50); }
      if (action === "start-camera") await this._startCamera();
      if (action === "stop-camera") this._stopCamera();
      if (action === "parse-manual") await this._parseManual();
      if (action === "manual-add") { this._form = this._blankForm(); this._render(); }
      if (action === "clear-search") { this._searchQuery = ""; this._render(); setTimeout(() => this.shadowRoot.getElementById("cabinet-search")?.focus({ preventScroll:true }), 0); }
      if (action === "close-modal") { this._form = null; this._render(); }
      if (action === "save-package") await this._savePackage();
      if (action === "lookup-form") await this._lookupFormData();
      if (action === "edit") this._editPackage(el.dataset.id);
      if (action === "consume-one") await this._confirmConsumeOne(el.dataset.id);
      if (action === "consume") await this._promptConsume(el.dataset.id);
      if (action === "delete-package") await this._deletePackage(el.dataset.id);
      if (action === "instruction") window.open(el.dataset.url, "_blank", "noopener");
      if (action === "local-instruction") await this._openInstruction(el.dataset.gtin);
      if (action === "analogs") await this._openAnalogs(el.dataset.gtin);
      if (action === "prices") await this._openPrices(el.dataset.id);
      if (action === "medication-link") await this._openMedicationLink(el.dataset.id);
      if (action === "close-medication-link") { this._medLink = null; this._render(); }
      if (action === "create-medication-schedule") await this._createMedicationSchedule();
      if (action === "link-existing-medication") await this._linkExistingMedication();
      if (action === "unlink-medication") await this._unlinkMedication(el.dataset.medicationId);
      if (action === "refresh-prices") await this._refreshPrices(true);
      if (action === "open-brief") this._openBriefViewer(el.dataset.id);
      if (action === "close-brief") { this._briefView = null; this._render(); }
      if (action === "refresh-briefs") { this._toast("Обновляю краткую инструкцию с RLSnet.ru…"); await this._refreshLiveBriefs(true); }
      if (action === "edit-brief") this._openBriefEditor(el.dataset.id);
      if (action === "close-brief-editor") { this._briefEdit = null; this._render(); }
      if (action === "save-brief") await this._saveBrief();
      if (action === "reset-brief") await this._resetBrief();
      if (action === "pharmacy") {
        const query = el.dataset.query || "";
        window.open(el.dataset.url, "_blank", "noopener");
        if (el.dataset.copy === "1" && query && navigator.clipboard?.writeText) {
          try { await navigator.clipboard.writeText(query); this._toast("Запрос скопирован — вставь его в поиск аптеки"); } catch (_) { this._toast(`Поиск: ${query}`); }
        }
      }
      if (action === "close-instruction") { this._instruction = null; this._render(); }
      if (action === "close-analogs") { this._analogs = null; this._render(); }
      if (action === "close-prices") { this._prices = null; this._priceResults = null; this._priceError = ""; this._render(); }
      if (action === "add-storage-location") await this._addStorageLocation(false);
      if (action === "save-settings") await this._saveSettings();
    } catch (err) {
      this._toast(err?.message || String(err), true);
    }
  }

  _hasNativeBarcodeScanner() {
    return Boolean(
      this._hass?.auth?.external?.config?.hasBarCodeScanner &&
      typeof this._hass?.auth?.external?.fireMessage === "function"
    );
  }

  async _startCamera() {
    this._stopCamera();

    // Home Assistant Companion App exposes its own native barcode scanner.
    // Prefer it over getUserMedia: it owns the Android camera permission and
    // works even when the embedded WebView doesn't expose navigator.mediaDevices.
    if (this._hasNativeBarcodeScanner()) {
      this._startNativeBarcodeScanner();
      return;
    }

    const video = this.shadowRoot.getElementById("scanner-video");
    const status = this.shadowRoot.getElementById("scan-status");
    const placeholder = this.shadowRoot.getElementById("camera-placeholder");
    if (!video) return;
    if (!navigator.mediaDevices?.getUserMedia) {
      status.textContent = "Камера недоступна в этом браузере. Открой Home Assistant Companion App либо используй HTTPS/резервный ввод.";
      return;
    }
    if (!("BarcodeDetector" in globalThis)) {
      status.textContent = "Этот браузер не поддерживает BarcodeDetector. Используй Home Assistant Companion App или резервный ввод.";
      return;
    }
    try {
      const supported = await BarcodeDetector.getSupportedFormats();
      const wanted = ["data_matrix", "ean_13", "ean_8", "code_128", "itf", "qr_code"];
      const formats = wanted.filter((x) => supported.includes(x));
      if (!formats.length) throw new Error("Браузер не сообщил поддерживаемые форматы штрихкодов");
      this._scanner = new BarcodeDetector({ formats });
      this._stream = await navigator.mediaDevices.getUserMedia({
        video: { facingMode: { ideal: "environment" }, width: { ideal: 1920 }, height: { ideal: 1080 } },
        audio: false,
      });
      video.srcObject = this._stream;
      await video.play();
      if (placeholder) placeholder.style.display = "none";
      status.textContent = `Камера активна · ${formats.join(", ")}`;
      this._scanLoop();
    } catch (err) {
      status.textContent = `Камера не запущена: ${err?.message || err}. Проверь разрешение камеры и HTTPS.`;
      this._stopCamera(false);
    }
  }

  _startNativeBarcodeScanner() {
    const external = this._hass?.auth?.external;
    const status = this.shadowRoot?.getElementById("scan-status");
    if (!external?.fireMessage) {
      if (status) status.textContent = "Нативный сканер Home Assistant недоступен.";
      return;
    }

    if (!this._installNativeBarcodeListener()) {
      if (status) status.textContent = "Не удалось подключиться к системному сканеру Home Assistant.";
      return;
    }

    const request = {
      type: "bar_code/scan",
      payload: {
        title: "Домашняя аптечка",
        description: "Наведи камеру на Data Matrix или штрихкод упаковки лекарства",
        alternative_option_label: "Ввести код вручную",
      },
    };

    this._nativeScannerActive = true;
    external.fireMessage(request);
    this._nativeScanMessageId = request.id ?? null;
    if (status) status.textContent = "Открыт системный сканер Home Assistant · Data Matrix / EAN / Code 128 / QR";
  }

  _installNativeBarcodeListener() {
    if (this._nativeExternalBusWrapper) return true;
    if (typeof window.externalBus !== "function") return false;

    const original = window.externalBus;
    this._nativeOriginalExternalBus = original;
    this._nativeExternalBusWrapper = (incoming) => {
      try {
        const msg = typeof incoming === "string" ? JSON.parse(incoming) : incoming;
        if (
          this._nativeScannerActive &&
          msg?.type === "command" &&
          (this._nativeScanMessageId == null || msg.id === this._nativeScanMessageId)
        ) {
          if (msg.command === "bar_code/scan_result") {
            const rawValue = msg.payload?.rawValue ?? "";
            const format = msg.payload?.format ?? "";
            queueMicrotask(async () => {
              this._closeNativeBarcodeScanner(false);
              if (!rawValue) {
                this._toast("Сканер вернул пустой код", true);
                return;
              }
              if (navigator.vibrate) navigator.vibrate(80);
              try {
                await this._handleScannedCode(rawValue, format);
              } catch (err) {
                this._toast(err?.message || String(err), true);
              }
            });
          } else if (msg.command === "bar_code/aborted") {
            const reason = msg.payload?.reason;
            queueMicrotask(() => {
              this._closeNativeBarcodeScanner(false, false);
              const scanStatus = this.shadowRoot?.getElementById("scan-status");
              if (scanStatus) {
                scanStatus.textContent = reason === "alternative_options"
                  ? "Сканирование закрыто — можно ввести код вручную ниже."
                  : "Сканирование отменено.";
              }
              if (reason === "alternative_options") {
                const input = this.shadowRoot?.getElementById("manual-code");
                input?.focus();
                input?.scrollIntoView({ behavior: "smooth", block: "center" });
              }
            });
          }
        }
      } catch (_) {
        // Never block Home Assistant's own external-bus handler.
      }
      return original(incoming);
    };

    window.externalBus = this._nativeExternalBusWrapper;
    return true;
  }

  _restoreNativeBarcodeListener() {
    if (this._nativeExternalBusWrapper && window.externalBus === this._nativeExternalBusWrapper && this._nativeOriginalExternalBus) {
      window.externalBus = this._nativeOriginalExternalBus;
    }
    this._nativeExternalBusWrapper = null;
    this._nativeOriginalExternalBus = null;
  }

  _closeNativeBarcodeScanner(updateStatus = true, sendClose = true) {
    if (this._nativeScannerActive && sendClose) {
      try {
        this._hass?.auth?.external?.fireMessage({ type: "bar_code/close" });
      } catch (_) {
        // Scanner may already have been dismissed by Android.
      }
    }
    this._nativeScannerActive = false;
    this._nativeScanMessageId = null;
    this._restoreNativeBarcodeListener();
    const status = this.shadowRoot?.getElementById("scan-status");
    if (status && updateStatus) status.textContent = "Сканер остановлен.";
  }

  _stopCamera(updateStatus = true) {
    this._closeNativeBarcodeScanner(false);
    if (this._scanFrame) cancelAnimationFrame(this._scanFrame);
    this._scanFrame = null;
    this._scanBusy = false;
    if (this._stream) this._stream.getTracks().forEach((t) => t.stop());
    this._stream = null;
    this._scanner = null;
    const video = this.shadowRoot?.getElementById("scanner-video");
    if (video) video.srcObject = null;
    const placeholder = this.shadowRoot?.getElementById("camera-placeholder");
    if (placeholder) placeholder.style.display = "grid";
    const status = this.shadowRoot?.getElementById("scan-status");
    if (status && updateStatus) status.textContent = "Сканер остановлен.";
  }

  _scanLoop() {
    const video = this.shadowRoot.getElementById("scanner-video");
    if (!video || !this._scanner || !this._stream) return;
    const tick = async () => {
      if (!this._scanner || !this._stream) return;
      if (!this._scanBusy && video.readyState >= 2) {
        this._scanBusy = true;
        try {
          const codes = await this._scanner.detect(video);
          if (codes?.length) {
            const code = codes[0];
            if (navigator.vibrate) navigator.vibrate(80);
            this._stopCamera(false);
            await this._handleScannedCode(code.rawValue, code.format);
            return;
          }
        } catch (_) {
          // Keep scanning: transient detect errors are common while the camera focuses.
        } finally {
          this._scanBusy = false;
        }
      }
      this._scanFrame = requestAnimationFrame(tick);
    };
    this._scanFrame = requestAnimationFrame(tick);
  }

  async _handleScannedCode(rawValue, format = "") {
    const status = this.shadowRoot?.getElementById("scan-status");
    if (status) status.textContent = "Код распознан. Загружаю данные о препарате…";
    const parsed = await this._hass.callWS({ type: "medicine_cabinet/parse_code", raw_code: rawValue, format });

    // Native scanners can catch a small EAN-8 printed near the medicine code.
    // Do not distract the user with an empty medicine form in that case.
    if ((format || "").toLowerCase() === "ean_8" && !parsed.catalog) {
      this._form = null;
      this._render();
      this._toast("Считан короткий EAN‑8. Наведи камеру на квадратный Data Matrix лекарства.", true);
      const scanStatus = this.shadowRoot?.getElementById("scan-status");
      if (scanStatus) scanStatus.textContent = "Нужен квадратный Data Matrix на упаковке лекарства.";
      return;
    }

    if (parsed.duplicate_package) {
      const d = parsed.duplicate_package;
      const title = [d.name, d.strength].filter(Boolean).join(" ");
      const where = d.location ? ` · ${d.location}` : "";
      const message = `Эта упаковка уже есть: ${title || "препарат"}${where}`;
      this._form = null;
      if (status) status.textContent = message;
      this._toast(message, true);
      return;
    }

    this._form = this._blankForm(parsed);
    this._render();
    if (parsed.lookup_status === "local") {
      if (parsed.expiry_source === "datamatrix_ai17") this._toast(`Данные и срок годности считаны из Data Matrix`);
      else this._toast(`Данные загружены. В Data Matrix срока годности нет — введи дату с коробки.`, true);
    } else this._toast(`Код распознан${format ? `: ${format}` : ""}. ${parsed.lookup_source || "Данные не найдены"} — можно заполнить вручную.`, true);
  }

  async _parseManual() {
    const input = this.shadowRoot.getElementById("manual-code");
    const raw = input?.value?.trim();
    if (!raw) return this._toast("Вставь значение кода", true);
    await this._handleScannedCode(raw, "ручной ввод");
  }

  _editPackage(id) {
    const pkg = (this._state.packages || []).find((p) => p.id === id);
    if (!pkg) return;
    this._form = { ...pkg };
    this._render();
  }

  _collectForm() {
    const modal = this.shadowRoot.querySelector(".modal");
    const out = { ...this._form };
    modal.querySelectorAll("input[name], textarea[name], select[name]").forEach((el) => { out[el.name] = el.value; });
    return out;
  }

  async _lookupFormData() {
    const current = this._collectForm();
    const gtin = current.gtin?.trim();
    if (!gtin) return this._toast("Сначала нужен GTIN", true);
    this._toast("Загружаю данные о препарате…");
    const result = await this._hass.callWS({
      type: "medicine_cabinet/lookup_product",
      gtin,
      force: true,
      raw_code: current.raw_code || "",
      format: current.barcode_format || "",
    });
    if (!result.catalog) return this._toast(result.lookup_source || "Препарат не найден в локальном каталоге", true);
    const cat = result.catalog;
    this._form = { ...current, ...cat, id: current.id || null, gtin: current.gtin, raw_code: current.raw_code, serial: current.serial, lot: current.lot, expiry: current.expiry, remaining: current.remaining || cat.package_size || "", package_size: cat.package_size || current.package_size };
    this._render();
    this._toast(`Данные обновлены: ${result.lookup_source || cat.source || "локальный каталог"}`);
  }

  async _savePackage() {
    const data = this._collectForm();
    if (!data.name?.trim()) return this._toast("Укажи название лекарства", true);
    if (data.id) {
      await this._hass.callWS({ type: "medicine_cabinet/update_package", package_id: data.id, package: data });
    } else {
      await this._hass.callWS({ type: "medicine_cabinet/add_package", package: data });
    }
    this._form = null;
    await this._loadState();
    this._toast("Упаковка сохранена");
  }

  async _consume(id, amount) {
    await this._hass.callWS({ type: "medicine_cabinet/consume", package_id: id, amount, reason: "Принято / использовано" });
    await this._loadState();
    this._toast(`Списано: ${this._fmtNumber(amount)}`);
  }

  _consumeConfirmText(pkg, amount) {
    const remaining = Number(pkg?.remaining || 0);
    const after = Math.max(0, remaining - Number(amount || 0));
    const unit = pkg?.unit || "шт.";
    return `Подтвердить списание из «${pkg?.name || "препарат"}»?\n\n` +
      `Списать: ${this._fmtNumber(amount)} ${unit}\n` +
      `Сейчас: ${this._fmtNumber(remaining)} ${unit}\n` +
      `Останется: ${this._fmtNumber(after)} ${unit}`;
  }

  async _confirmConsumeOne(id) {
    const pkg = (this._state.packages || []).find((p) => p.id === id);
    if (!pkg) return;
    if (Number(pkg.remaining || 0) <= 0) return this._toast("Списывать нечего", true);
    if (!window.confirm(this._consumeConfirmText(pkg, 1))) return;
    await this._consume(id, 1);
  }

  async _promptConsume(id) {
    const pkg = (this._state.packages || []).find((p) => p.id === id);
    if (!pkg) return;
    const raw = window.prompt(`Сколько списать? Сейчас ${this._fmtNumber(pkg.remaining)} ${pkg.unit}`, "1");
    if (raw === null) return;
    const amount = Number(String(raw).replace(",", "."));
    if (!Number.isFinite(amount) || amount <= 0) return this._toast("Некорректное количество", true);
    if (!window.confirm(this._consumeConfirmText(pkg, amount))) return;
    await this._consume(id, amount);
  }

  async _deletePackage(id) {
    if (!window.confirm("Удалить эту упаковку из аптечки? История операции сохранится.")) return;
    await this._hass.callWS({ type: "medicine_cabinet/delete_package", package_id: id });
    this._form = null;
    await this._loadState();
    this._toast("Упаковка удалена");
  }

  async _saveSettings() {
    const days = Number(this.shadowRoot.getElementById("expiry-warning")?.value || 30);
    const enabled = Boolean(this.shadowRoot.getElementById("notifications-enabled")?.checked);
    const priceCity = (this.shadowRoot.getElementById("price-city")?.value || "Москва").trim();
    await this._hass.callWS({ type: "medicine_cabinet/update_settings", settings: { expiry_warning_days: Math.max(1, Math.min(365, days)), notifications_enabled: enabled, price_city: priceCity || "Москва" } });
    await this._loadState();
    this._tab = "settings";
    this._render();
    this._toast("Настройки сохранены");
  }

  _toast(message, error = false) {
    const toast = this.shadowRoot?.getElementById("toast");
    if (!toast) return;
    toast.textContent = message;
    toast.className = `toast show ${error ? "toast-error" : ""}`;
    clearTimeout(this._toastTimer);
    this._toastTimer = setTimeout(() => { if (toast) toast.className = "toast"; }, 2800);
  }

  _styles() {
    return `
      :host { display:block; height:100%; color:var(--primary-text-color); background:var(--primary-background-color); font-family:var(--paper-font-body1_-_font-family, Roboto, system-ui, sans-serif); }
      * { box-sizing:border-box; }
      button,input,textarea,select { font:inherit; }
      button { color:inherit; }
      .app { min-height:100%; padding:18px clamp(12px,2vw,28px) 40px; background:linear-gradient(145deg, color-mix(in srgb, var(--primary-background-color) 94%, var(--primary-color) 6%), var(--primary-background-color)); }
      .topbar { display:flex; align-items:center; justify-content:space-between; gap:16px; max-width:1500px; margin:0 auto 18px; }
      .title-wrap { display:flex; align-items:center; gap:12px; min-width:0; }
      .brand-icon { width:44px; height:44px; border-radius:14px; display:grid; place-items:center; background:color-mix(in srgb, var(--primary-color) 16%, var(--card-background-color)); color:var(--primary-color); font-size:25px; font-weight:700; border:1px solid color-mix(in srgb,var(--primary-color) 25%,transparent); }
      .title { font-size:clamp(20px,2vw,27px); font-weight:750; letter-spacing:-.02em; }
      .subtitle { margin-top:2px; color:var(--secondary-text-color); font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .top-actions { display:flex; gap:8px; }
      .btn,.small-btn,.icon-btn,.tab,.summary-card { border:0; cursor:pointer; transition:.16s ease; }
      .btn { min-height:42px; padding:0 16px; border-radius:13px; font-weight:650; display:inline-flex; align-items:center; justify-content:center; gap:7px; }
      .btn:hover,.small-btn:hover,.summary-card:hover { transform:translateY(-1px); }
      .primary { background:var(--primary-color); color:var(--text-primary-color,#fff); box-shadow:0 8px 24px color-mix(in srgb,var(--primary-color) 22%,transparent); }
      .ghost { background:var(--card-background-color); border:1px solid var(--divider-color); }
      .big { min-height:50px; padding-inline:22px; }
      .icon-btn { width:40px; height:40px; border-radius:12px; background:transparent; font-size:25px; }
      .icon-btn:hover { background:color-mix(in srgb,var(--primary-text-color) 8%,transparent); }
      .menu-btn { display:none; font-size:22px; }
      .summary-grid { max-width:1500px; margin:0 auto 16px; display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:10px; }
      .summary-card { min-height:88px; text-align:left; padding:15px 17px; border-radius:18px; background:color-mix(in srgb,var(--card-background-color) 92%,transparent); border:1px solid var(--divider-color); box-shadow:0 6px 22px rgba(0,0,0,.035); display:flex; flex-direction:column; justify-content:center; }
      .summary-value { font-size:28px; line-height:1; font-weight:800; }
      .summary-label { margin-top:7px; color:var(--secondary-text-color); font-size:13px; }
      .tone-danger .summary-value { color:var(--error-color,#db4437); }
      .tone-warning .summary-value { color:var(--warning-color,#f2a600); }
      .tone-cart .summary-value { color:var(--primary-color); }
      .tabs { position:sticky; top:0; z-index:5; max-width:1500px; margin:0 auto 16px; padding:6px; border-radius:17px; background:color-mix(in srgb,var(--card-background-color) 90%,transparent); backdrop-filter:blur(14px); border:1px solid var(--divider-color); display:flex; gap:4px; overflow:auto; }
      .tab { min-width:max-content; min-height:42px; padding:0 14px; border-radius:12px; background:transparent; display:flex; gap:7px; align-items:center; color:var(--secondary-text-color); font-weight:600; }
      .tab.active { background:color-mix(in srgb,var(--primary-color) 13%,var(--card-background-color)); color:var(--primary-color); }
      .content { max-width:1500px; margin:0 auto; }
      .section-head { display:flex; align-items:end; justify-content:space-between; gap:16px; margin:8px 2px 14px; }
      h2,h3,p { margin-top:0; }
      .section-head h2 { margin:0 0 4px; font-size:20px; }
      .section-head p,.manual-card p { margin:0; color:var(--secondary-text-color); font-size:14px; }
      .cabinet-search-panel { position:sticky; top:62px; z-index:4; display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:9px; margin:0 0 10px; padding:9px 11px; border:1px solid color-mix(in srgb,var(--primary-color) 22%,var(--divider-color)); border-radius:15px; background:color-mix(in srgb,var(--card-background-color) 94%,transparent); backdrop-filter:blur(14px); box-shadow:0 7px 24px rgba(0,0,0,.08); }
      .cabinet-search-panel .search-icon { color:var(--primary-color); font-size:22px; line-height:1; transform:rotate(-18deg); }
      .cabinet-search-panel input { width:100%; min-height:36px; border:0; outline:0; background:transparent; color:var(--primary-text-color); font-size:15px; }
      .cabinet-search-panel input::placeholder { color:var(--secondary-text-color); opacity:.78; }
      .search-clear { width:31px; height:31px; border:0; border-radius:10px; cursor:pointer; color:var(--secondary-text-color); background:color-mix(in srgb,var(--secondary-text-color) 8%,transparent); font-size:21px; line-height:1; }
      .cabinet-tools { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:end; margin-bottom:14px; }
      .category-scroll { display:flex; gap:7px; overflow:auto; padding:2px 1px 6px; scrollbar-width:thin; }
      .category-chip { flex:0 0 auto; border:1px solid var(--divider-color); background:var(--card-background-color); color:var(--secondary-text-color); min-height:36px; padding:0 12px; border-radius:999px; cursor:pointer; white-space:nowrap; }
      .category-chip.active { border-color:color-mix(in srgb,var(--primary-color) 45%,var(--divider-color)); background:color-mix(in srgb,var(--primary-color) 12%,var(--card-background-color)); color:var(--primary-text-color); }
      .category-chip b { margin-left:4px; font-size:11px; opacity:.7; }
      .sort-control { display:grid; gap:5px; color:var(--secondary-text-color); font-size:11.5px; white-space:nowrap; }
      select { border:1px solid var(--divider-color); background:var(--primary-background-color); color:var(--primary-text-color); border-radius:11px; padding:10px 11px; outline:none; }
      .medicine-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(300px,1fr)); gap:12px; }
      .medicine-card { padding:17px; border-radius:20px; background:var(--card-background-color); border:1px solid var(--divider-color); box-shadow:0 8px 28px rgba(0,0,0,.035); }
      .card-top { display:flex; justify-content:space-between; align-items:center; }
      .pill-icon { width:38px; height:38px; border-radius:12px; display:grid; place-items:center; background:color-mix(in srgb,var(--primary-color) 10%,transparent); }
      .status { font-size:12px; font-weight:750; padding:5px 9px; border-radius:999px; }
      .status.ok { color:var(--success-color,#2eaa62); background:color-mix(in srgb,var(--success-color,#2eaa62) 13%,transparent); }
      .status.warning { color:var(--warning-color,#d99100); background:color-mix(in srgb,var(--warning-color,#d99100) 14%,transparent); }
      .status.danger { color:var(--error-color,#db4437); background:color-mix(in srgb,var(--error-color,#db4437) 13%,transparent); }
      .status.muted { color:var(--secondary-text-color); background:color-mix(in srgb,var(--secondary-text-color) 11%,transparent); }
      .category-label { margin-top:10px; color:var(--secondary-text-color); font-size:11.5px; font-weight:650; }
      .category-group { margin:0 0 22px; }.category-group-head { display:flex; justify-content:space-between; align-items:center; margin:0 2px 10px; padding:0 2px; }.category-group-head>div { display:flex; gap:8px; align-items:center; font-size:16px; }.category-group-head>span { min-width:28px; text-align:center; padding:3px 8px; border-radius:999px; background:color-mix(in srgb,var(--primary-color) 10%,transparent); color:var(--primary-color); font-size:12px; font-weight:800; }.category-group-icon { font-size:18px; }
      .brief-instruction { margin-top:13px; padding:11px 12px; border:1px solid color-mix(in srgb,var(--primary-color) 20%,var(--divider-color)); border-radius:13px; background:color-mix(in srgb,var(--primary-color) 5%,transparent); display:grid; gap:7px; }.brief-head { color:var(--primary-color); font-size:12px; font-weight:800; }.brief-instruction>div:not(.brief-head) { display:grid; gap:2px; font-size:11.5px; line-height:1.35; }.brief-instruction>div:not(.brief-head) b { color:var(--primary-text-color); }.brief-instruction>div:not(.brief-head) span,.brief-instruction>span,.brief-instruction small { color:var(--secondary-text-color); }.brief-head { display:flex; align-items:center; gap:6px; flex-wrap:wrap; }.brief-head em { font-style:normal; font-size:9px; padding:2px 6px; border-radius:999px; background:color-mix(in srgb,var(--primary-color) 11%,transparent); color:var(--primary-color); }.brief-instruction.live { border-color:color-mix(in srgb,var(--success-color,#4caf50) 28%,var(--divider-color)); }.brief-instruction.live .brief-head { color:var(--success-color,#4caf50); }.brief-instruction.fallback { opacity:.9; }.brief-instruction small { font-size:9.5px; line-height:1.3; }.brief-instruction.unavailable { opacity:.76; }.instruction-btn { border-color:color-mix(in srgb,var(--primary-color) 35%,var(--divider-color)); color:var(--primary-color); }
      .medicine-card h3 { margin:5px 0 5px; font-size:18px; line-height:1.25; }
      .strength { color:var(--primary-color); font-weight:700; }
      .meta,.muted-text { color:var(--secondary-text-color); font-size:13px; }
      .stock-row { display:flex; justify-content:space-between; align-items:end; margin-top:16px; }
      .stock-num { font-size:24px; font-weight:800; }
      .progress { height:6px; border-radius:20px; overflow:hidden; background:color-mix(in srgb,var(--secondary-text-color) 13%,transparent); margin:7px 0 14px; }
      .progress i { display:block; height:100%; background:var(--primary-color); border-radius:inherit; }
      .details { display:grid; gap:6px; color:var(--secondary-text-color); font-size:12.5px; }
      .card-actions { display:flex; flex-wrap:wrap; gap:6px; margin-top:15px; }
      .small-btn { min-height:34px; padding:0 10px; border-radius:10px; background:color-mix(in srgb,var(--primary-background-color) 65%,var(--card-background-color)); border:1px solid var(--divider-color); font-size:12.5px; }
      .empty { min-height:330px; display:flex; flex-direction:column; align-items:center; justify-content:center; text-align:center; gap:10px; padding:28px; border-radius:22px; background:var(--card-background-color); border:1px solid var(--divider-color); }
      .empty h2 { margin:0; }.empty p { max-width:560px; margin:0 0 8px; color:var(--secondary-text-color); }.big-icon { font-size:50px; }
      .spinner { width:36px; height:36px; border:3px solid var(--divider-color); border-top-color:var(--primary-color); border-radius:50%; animation:spin .8s linear infinite; } @keyframes spin{to{transform:rotate(360deg)}}
      .scanner-layout { display:grid; grid-template-columns:minmax(0,1.5fr) minmax(270px,.5fr); gap:12px; }
      .scanner-card,.manual-card,.settings-card { border-radius:22px; background:var(--card-background-color); border:1px solid var(--divider-color); padding:18px; }
      .video-wrap { position:relative; min-height:440px; border-radius:18px; overflow:hidden; background:#080a0d; display:grid; place-items:center; }
      #scanner-video { width:100%; height:100%; min-height:440px; object-fit:cover; }
      .camera-placeholder { position:absolute; inset:0; display:grid; place-items:center; align-content:center; color:#fff; font-size:46px; text-align:center; background:radial-gradient(circle at 50% 40%,#2b3038,#090b0e); }
      .camera-placeholder span { font-size:14px; color:#c9ced6; margin-top:10px; }
      .scan-frame { pointer-events:none; position:absolute; width:min(72%,430px); aspect-ratio:1/1; inset:50% auto auto 50%; transform:translate(-50%,-50%); }
      .scan-frame i { position:absolute; width:42px; height:42px; border-color:#fff; border-style:solid; filter:drop-shadow(0 2px 5px #000); }
      .scan-frame i:nth-child(1){left:0;top:0;border-width:3px 0 0 3px;border-radius:12px 0 0 0}.scan-frame i:nth-child(2){right:0;top:0;border-width:3px 3px 0 0;border-radius:0 12px 0 0}.scan-frame i:nth-child(3){left:0;bottom:0;border-width:0 0 3px 3px;border-radius:0 0 0 12px}.scan-frame i:nth-child(4){right:0;bottom:0;border-width:0 3px 3px 0;border-radius:0 0 12px 0}
      .scan-status { margin:12px 2px; color:var(--secondary-text-color); font-size:13px; }.scanner-actions { display:flex; gap:8px; flex-wrap:wrap; }
      .manual-card textarea,.form-grid textarea { width:100%; min-height:110px; resize:vertical; }.manual-card textarea { margin:8px 0; }
      textarea,input,select { border:1px solid var(--divider-color); background:var(--primary-background-color); color:var(--primary-text-color); border-radius:11px; padding:10px 11px; outline:none; }
      textarea:focus,input:focus,select:focus { border-color:var(--primary-color); box-shadow:0 0 0 2px color-mix(in srgb,var(--primary-color) 13%,transparent); }
      .secure-note { margin-top:16px; padding:12px; border-radius:12px; background:color-mix(in srgb,var(--primary-color) 7%,transparent); color:var(--secondary-text-color); font-size:12px; line-height:1.45; }
      .shopping-list,.history-list { display:grid; gap:8px; }.shopping-item,.history-row { display:flex; align-items:center; gap:12px; padding:14px 16px; border-radius:16px; background:var(--card-background-color); border:1px solid var(--divider-color); }
      .shopping-item>div,.history-main { flex:1; min-width:0; display:grid; gap:3px; }.shopping-item span,.history-main span,.history-side span { color:var(--secondary-text-color); font-size:12.5px; }.history-dot { width:9px;height:9px;border-radius:50%;background:var(--primary-color);flex:0 0 auto}.history-side { display:grid; text-align:right; gap:3px; }
      .settings-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }.settings-card label { display:grid; gap:7px; margin:14px 0; font-size:13px; font-weight:650; }.settings-card input[type=number] { max-width:180px; }.switch-row { grid-template-columns:1fr auto!important; align-items:center; }.switch-row span { display:grid; gap:4px; }.switch-row small { color:var(--secondary-text-color); font-weight:400; line-height:1.4; }.switch-row input { width:22px;height:22px; }.catalog-card { grid-column:1/-1; }.catalog-stats { display:grid; grid-template-columns:repeat(6,minmax(110px,1fr)); gap:8px; margin:14px 0 10px; }.catalog-stat { min-height:88px; padding:12px; border-radius:14px; background:color-mix(in srgb,var(--primary-background-color) 70%,var(--card-background-color)); border:1px solid var(--divider-color); display:flex; flex-direction:column; justify-content:center; }.catalog-stat b { font-size:23px; color:var(--primary-color); }.catalog-stat span { font-size:11.5px; font-weight:700; margin-top:2px; }.catalog-stat small,.catalog-meta { color:var(--secondary-text-color); font-size:10.5px; margin-top:3px; }
      code { padding:2px 5px; border-radius:6px; background:color-mix(in srgb,var(--secondary-text-color) 10%,transparent); }
      .modal-backdrop { position:fixed; inset:0; z-index:30; background:rgba(0,0,0,.5); backdrop-filter:blur(6px); display:grid; place-items:center; padding:12px; }
      .modal { width:min(900px,100%); max-height:94vh; overflow:auto; background:var(--card-background-color); color:var(--primary-text-color); border-radius:24px; padding:20px; box-shadow:0 26px 80px rgba(0,0,0,.35); border:1px solid var(--divider-color); }
      .modal-head { display:flex; align-items:start; justify-content:space-between; gap:12px; margin-bottom:14px; }.modal-head h2 { margin:0 0 3px; }.modal-head p { margin:0; color:var(--secondary-text-color); font-size:13px; }
      .form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:11px; }.form-grid label { display:grid; gap:6px; color:var(--secondary-text-color); font-size:12.5px; }.form-grid input,.form-grid textarea,.form-grid select { color:var(--primary-text-color); font-size:14px; }.field-note{font-size:10.5px;line-height:1.35;color:var(--secondary-text-color)}.field-note.ok{color:var(--success-color,#4caf50)}
      .lookup-source { margin-top:13px; padding:10px 12px; border-radius:11px; background:color-mix(in srgb,var(--primary-color) 8%,transparent); color:var(--secondary-text-color); font-size:12px; }
      .lookup-source a { color:var(--primary-color); }.form-grid .full { grid-column:1/-1; }
      .instruction-modal { width:min(980px,100%); }.instruction-sections { display:grid; gap:10px; }.instruction-sections section { border:1px solid var(--divider-color); border-radius:14px; padding:13px 14px; background:color-mix(in srgb,var(--primary-background-color) 55%,var(--card-background-color)); }.instruction-sections h3 { margin:0 0 8px; font-size:15px; }.instruction-sections section div { white-space:pre-wrap; line-height:1.55; color:var(--primary-text-color); font-size:13.5px; }.modal-actions { display:flex; flex-wrap:wrap; align-items:center; gap:8px; margin-top:18px; }.modal-actions span { flex:1; }.danger-btn { background:color-mix(in srgb,var(--error-color,#db4437) 13%,transparent); color:var(--error-color,#db4437); }
      .rls-btn { border-color:color-mix(in srgb,var(--primary-color) 42%,var(--divider-color)); color:var(--primary-color); }.prices-btn { border-color:color-mix(in srgb,#22c55e 38%,var(--divider-color)); color:color-mix(in srgb,#22c55e 82%,var(--primary-text-color)); }.analogs-btn { border-color:color-mix(in srgb,#8b5cf6 38%,var(--divider-color)); }
      .external-info,.analog-warning { padding:13px 14px; border-radius:14px; border:1px solid color-mix(in srgb,var(--warning-color,#ff9800) 30%,var(--divider-color)); background:color-mix(in srgb,var(--warning-color,#ff9800) 7%,transparent); display:grid; gap:5px; line-height:1.45; }.external-info span,.analog-warning { color:var(--secondary-text-color); font-size:12.5px; }
      .instruction-footer { display:flex; align-items:center; gap:12px; margin-top:15px; padding-top:13px; border-top:1px solid var(--divider-color); }.instruction-footer>span { flex:1; color:var(--secondary-text-color); font-size:11.5px; }.instruction-footer>div { display:flex; flex-wrap:wrap; gap:8px; }
      .analogs-modal { width:min(1050px,100%); }.analog-section { margin-top:16px; }.analog-section-head { display:flex; justify-content:space-between; align-items:flex-start; gap:10px; margin-bottom:8px; }.analog-section-head h3 { margin:0 0 3px; }.analog-section-head p { margin:0; color:var(--secondary-text-color); font-size:11.5px; }.analog-section-head>b { min-width:30px; text-align:center; padding:4px 8px; border-radius:999px; background:color-mix(in srgb,var(--primary-color) 10%,transparent); color:var(--primary-color); }
      .prices-modal { width:min(760px,100%); }.price-warning { padding:13px 14px; margin-bottom:12px; border-radius:14px; border:1px solid color-mix(in srgb,var(--primary-color) 22%,var(--divider-color)); background:color-mix(in srgb,var(--primary-color) 6%,transparent); display:grid; gap:4px; }.price-warning span { color:var(--secondary-text-color); font-size:12px; line-height:1.45; }.pharmacy-list { display:grid; gap:8px; }.pharmacy-item { display:flex; align-items:center; gap:11px; padding:12px; border:1px solid var(--divider-color); border-radius:14px; background:color-mix(in srgb,var(--primary-background-color) 58%,var(--card-background-color)); }.pharmacy-icon { width:40px; height:40px; border-radius:12px; display:grid; place-items:center; font-size:21px; background:color-mix(in srgb,var(--primary-color) 8%,transparent); }.pharmacy-main { flex:1; min-width:0; display:grid; gap:3px; }.pharmacy-main span { color:var(--secondary-text-color); font-size:11.5px; line-height:1.35; }.price-toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; padding:10px 12px; border:1px solid var(--divider-color); border-radius:14px; }.price-toolbar>div { display:grid; gap:2px; }.price-toolbar span { color:var(--secondary-text-color); font-size:11px; }.price-value { font-size:18px; font-weight:800; color:var(--success-color,#4caf50); }.price-stock { color:var(--secondary-text-color); font-size:11px; }.price-missing { color:var(--secondary-text-color); font-size:11px; line-height:1.35; }.price-loading { display:flex; gap:7px; align-items:center; color:var(--secondary-text-color); font-size:11px; }.price-loading i { width:12px; height:12px; border:2px solid var(--divider-color); border-top-color:var(--primary-color); border-radius:50%; animation:spin .8s linear infinite; }.mini-refresh { border:0; background:transparent; color:var(--primary-color); font-size:16px; cursor:pointer; padding:0 3px; }.brief-head { display:flex!important; align-items:center; justify-content:space-between; gap:8px; }.brief-actions { display:flex; align-items:center; gap:5px; }.edit-brief-btn { font-size:15px; }.brief-instruction.custom { border-color:color-mix(in srgb,var(--primary-color) 38%,var(--divider-color)); background:color-mix(in srgb,var(--primary-color) 7%,transparent); }.brief-edit-modal { width:min(760px,100%); }.brief-edit-note { padding:11px 13px; border:1px solid color-mix(in srgb,var(--primary-color) 24%,var(--divider-color)); border-radius:13px; background:color-mix(in srgb,var(--primary-color) 6%,transparent); display:grid; gap:4px; margin-bottom:13px; }.brief-edit-note span { color:var(--secondary-text-color); font-size:12px; line-height:1.4; }.brief-edit-fields { display:grid; gap:11px; }.brief-edit-fields label { display:grid; gap:6px; color:var(--secondary-text-color); font-size:12.5px; }.brief-edit-fields textarea { width:100%; resize:vertical; min-height:88px; padding:10px 11px; border:1px solid var(--divider-color); border-radius:11px; background:var(--card-background-color); color:var(--primary-text-color); line-height:1.45; }.price-warning.error { border-color:color-mix(in srgb,var(--error-color,#f44336) 30%,var(--divider-color)); } .analog-list { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:8px; }.analog-item { display:flex; align-items:center; gap:10px; min-width:0; padding:11px 12px; border:1px solid var(--divider-color); border-radius:13px; background:color-mix(in srgb,var(--primary-background-color) 58%,var(--card-background-color)); }.analog-main { flex:1; min-width:0; display:grid; gap:3px; }.analog-main>b { overflow:hidden; text-overflow:ellipsis; }.analog-main>b span { color:var(--primary-color); }.analog-main small { color:var(--secondary-text-color); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }.match-chips { display:flex; flex-wrap:wrap; gap:4px; margin-top:2px; }.match-chips em { font-style:normal; font-size:9.5px; padding:2px 6px; border-radius:999px; background:color-mix(in srgb,var(--success-color,#4caf50) 12%,transparent); color:var(--success-color,#4caf50); }.analog-empty { padding:14px; border-radius:12px; color:var(--secondary-text-color); background:color-mix(in srgb,var(--secondary-text-color) 5%,transparent); }
      .location-settings-list { display:flex; flex-wrap:wrap; gap:7px; margin:10px 0 13px; }
      .location-settings-list span { padding:7px 10px; border-radius:999px; border:1px solid var(--divider-color); background:color-mix(in srgb,var(--primary-color) 5%,var(--card-background-color)); font-size:12px; }
      .medication-btn { border-color:color-mix(in srgb,#0ea5e9 38%,var(--divider-color)); color:color-mix(in srgb,#0ea5e9 82%,var(--primary-text-color)); }.med-link-modal { width:min(820px,100%); }.med-linked-banner,.med-link-info { display:grid; gap:4px; padding:12px 13px; border-radius:14px; margin-bottom:13px; border:1px solid color-mix(in srgb,var(--success-color,#4caf50) 30%,var(--divider-color)); background:color-mix(in srgb,var(--success-color,#4caf50) 7%,transparent); }.med-linked-banner span,.med-link-info span { color:var(--secondary-text-color); font-size:12px; line-height:1.45; }.med-field { display:grid; gap:6px; font-size:12.5px; color:var(--secondary-text-color); margin-bottom:12px; }.med-field select,.med-link-row select,.med-form-grid input,.med-form-grid select { width:100%; min-height:42px; padding:8px 10px; border:1px solid var(--divider-color); border-radius:10px; background:var(--card-background-color); color:var(--primary-text-color); }.med-link-section { border:1px solid var(--divider-color); border-radius:15px; padding:13px; margin:10px 0; background:color-mix(in srgb,var(--primary-background-color) 56%,var(--card-background-color)); }.med-link-section h3 { margin:0 0 4px; }.med-link-section>p { margin:0 0 11px; color:var(--secondary-text-color); font-size:12px; line-height:1.45; }.med-link-row { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:8px; }.med-form-grid { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; margin-bottom:11px; }.med-form-grid label { display:grid; gap:5px; color:var(--secondary-text-color); font-size:12px; }.med-check { display:flex!important; align-items:center; gap:8px!important; }.med-check input { width:20px!important; min-height:20px!important; }.med-check span { color:var(--primary-text-color); }.med-link-info { border-color:color-mix(in srgb,var(--primary-color) 24%,var(--divider-color)); background:color-mix(in srgb,var(--primary-color) 6%,transparent); margin-top:12px; }
      /* v0.4.3: compact aligned medicine cards without artificial empty space */
      .medicine-grid { align-items:stretch; }
      .medicine-card { display:flex; flex-direction:column; height:100%; }
      .card-top { display:grid; grid-template-columns:38px minmax(0,1fr) auto; gap:10px; align-items:center; min-height:38px; }
      .category-label { margin:0; min-width:0; color:var(--secondary-text-color); font-size:11.5px; font-weight:650; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .medicine-card h3 { margin:16px 0 5px; min-height:45px; max-height:45px; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:2; overflow:hidden; font-size:18px; line-height:1.25; }
      .medicine-card .meta { line-height:1.4; height:54.6px; min-height:54.6px; max-height:54.6px; display:-webkit-box; -webkit-box-orient:vertical; -webkit-line-clamp:3; overflow:hidden; }
      .brief-open-btn { width:100%; min-height:40px; margin-top:13px; border-radius:12px; border:1px solid color-mix(in srgb,var(--primary-color) 55%,var(--divider-color)); background:color-mix(in srgb,var(--primary-color) 6%,transparent); color:var(--primary-color); font-size:12.5px; font-weight:800; cursor:pointer; }
      .brief-open-btn:hover { background:color-mix(in srgb,var(--primary-color) 11%,transparent); }
      .stock-row { margin-top:16px; }
      .details { grid-template-rows:repeat(4,18px); min-height:90px; max-height:90px; overflow:hidden; }
      .details>span { min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
      .storage-line { white-space:nowrap!important; overflow:hidden; text-overflow:ellipsis; }
      .card-actions { margin-top:15px; padding-top:0; display:grid; gap:7px; }
      .action-row { display:grid; gap:7px; }
      .action-row-two { grid-template-columns:minmax(0,2fr) minmax(96px,1fr); }
      .action-row-three { grid-template-columns:minmax(0,1.55fr) minmax(0,1fr) minmax(56px,.55fr); }
      .action-row .small-btn { width:100%; height:38px; min-height:38px; padding:0 8px; display:flex; align-items:center; justify-content:center; text-align:center; line-height:1.15; }
      .small-btn:disabled,.small-btn[aria-disabled="true"] { cursor:not-allowed; color:var(--secondary-text-color)!important; border-color:var(--divider-color)!important; background:color-mix(in srgb,var(--secondary-text-color) 7%,var(--card-background-color))!important; opacity:.55; }
      .brief-view-modal { width:min(820px,100%); }
      .brief-view-sections { display:grid; gap:10px; }
      .brief-view-sections section { padding:13px 14px; border:1px solid var(--divider-color); border-radius:14px; background:color-mix(in srgb,var(--primary-background-color) 55%,var(--card-background-color)); }
      .brief-view-sections h3 { margin:0 0 6px; color:var(--primary-color); font-size:14px; }
      .brief-view-sections section div { color:var(--primary-text-color); white-space:pre-wrap; line-height:1.5; font-size:13px; }
      .brief-view-empty { padding:18px; border:1px solid var(--divider-color); border-radius:14px; color:var(--secondary-text-color); line-height:1.5; }
      .brief-view-actions { display:grid; grid-template-columns:1fr 1fr 1fr; gap:8px; margin-top:15px; }

      .toast { position:fixed; z-index:60; left:50%; bottom:24px; transform:translate(-50%,20px); opacity:0; pointer-events:none; padding:11px 16px; border-radius:12px; background:#202124; color:#fff; box-shadow:0 10px 30px rgba(0,0,0,.28); transition:.2s; }.toast.show { opacity:1; transform:translate(-50%,0); }.toast-error { background:#8d2521; }
      @media (max-width:870px) {
        .app { padding:10px 10px 28px; }.pharmacy-item { align-items:flex-start; flex-wrap:wrap; }.pharmacy-item .btn { margin-left:51px; }.price-toolbar { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:10px; padding:10px 12px; border:1px solid var(--divider-color); border-radius:14px; }.price-toolbar>div { display:grid; gap:2px; }.price-toolbar span { color:var(--secondary-text-color); font-size:11px; }.price-value { font-size:18px; font-weight:800; color:var(--success-color,#4caf50); }.price-stock { color:var(--secondary-text-color); font-size:11px; }.price-missing { color:var(--secondary-text-color); font-size:11px; line-height:1.35; }.price-loading { display:flex; gap:7px; align-items:center; color:var(--secondary-text-color); font-size:11px; }.price-loading i { width:12px; height:12px; border:2px solid var(--divider-color); border-top-color:var(--primary-color); border-radius:50%; animation:spin .8s linear infinite; }.mini-refresh { border:0; background:transparent; color:var(--primary-color); font-size:16px; cursor:pointer; padding:0 3px; }.brief-head { display:flex!important; align-items:center; justify-content:space-between; gap:8px; }.price-warning.error { border-color:color-mix(in srgb,var(--error-color,#f44336) 30%,var(--divider-color)); } .analog-list { grid-template-columns:1fr; }.instruction-footer { align-items:flex-start; flex-direction:column; }.menu-btn { display:grid; place-items:center; }.brand-icon { display:none; }.top-actions .ghost { display:none; }.subtitle { max-width:52vw; }.summary-grid { grid-template-columns:repeat(2,1fr); }.summary-card { min-height:72px; }.summary-value { font-size:24px; }.tabs { margin-inline:-2px; top:0; }.cabinet-search-panel { top:58px; }.scanner-layout,.settings-grid { grid-template-columns:1fr; }.catalog-card { grid-column:auto; }.catalog-stats { grid-template-columns:repeat(3,1fr); }.cabinet-tools { grid-template-columns:1fr; }.sort-control { width:100%; }.sort-control select { width:100%; }.video-wrap,#scanner-video { min-height:55vh; }.manual-card { order:2; }.medicine-grid { grid-template-columns:1fr; }.med-form-grid,.med-link-row { grid-template-columns:1fr; }
      }
      @media (max-width:560px) {
        .action-row-two { grid-template-columns:minmax(0,2fr) minmax(78px,1fr); }
        .action-row-three { grid-template-columns:minmax(0,1.55fr) minmax(0,1fr) minmax(52px,.55fr); }
        .action-row .small-btn { padding:0 6px; font-size:11.5px; }
        .brief-view-actions { grid-template-columns:1fr; }

        .topbar { align-items:flex-start; }.title { font-size:19px; }.subtitle { font-size:11.5px; max-width:58vw; }.top-actions .primary { min-width:44px; width:44px; padding:0; font-size:0; }.top-actions .primary::before { content:"▣"; font-size:20px; }.summary-grid { gap:7px; }.summary-card { padding:11px 13px; border-radius:15px; }.tabs .tab { padding-inline:12px; }.tabs .tab span:last-child { display:none; }.tabs .tab { min-width:46px; justify-content:center; font-size:18px; }.form-grid { grid-template-columns:1fr; }.form-grid .full { grid-column:auto; }.modal { padding:15px; border-radius:19px; }.modal-actions { grid-template-columns:1fr 1fr; }.modal-actions span { display:none; }.modal-actions .danger-btn { grid-column:1/-1; }.shopping-item { align-items:flex-start; flex-direction:column; }.catalog-stats { grid-template-columns:repeat(2,1fr); }.history-side { min-width:95px; }
      }
    `;
  }
}

if (!customElements.get("medicine-cabinet-panel-v044")) {
  customElements.define("medicine-cabinet-panel-v044", MedicineCabinetPanel);
}
