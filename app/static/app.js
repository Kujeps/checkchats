const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("ru-RU"));

const VERDICT = { live: "Живой", low: "Слабый", dead: "Мёртвый", spam: "Спам/боты", private: "Закрытый" };
const STATUS = { pending: "в очереди", checking: "проверяется", checked: "проверен", not_found: "не найден",
  not_chat: "не чат", private: "нет доступа", error: "ошибка", duplicate: "дубль" };
const ACC_STATUS = { new: "новый", active: "активен", flood: "FloodWait", dead: "мёртв", frozen: "заморожен",
  proxy_error: "нет связи", disabled: "отключён", working: "работает", idle: "ждёт задач", limit: "лимит на сегодня",
  connecting: "подключение", search: "ищет чаты", starting: "старт", stopped: "остановлен" };

let settings = null;
let page = 1;
const PAGE_SIZE = 50;

function toast(msg, ms = 3500) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.hidden = true), ms);
}

async function api(path, opts = {}) {
  const o = { ...opts };
  if (o.json !== undefined) {
    o.method = o.method || "POST";
    o.headers = { "Content-Type": "application/json" };
    o.body = JSON.stringify(o.json);
    delete o.json;
  }
  const r = await fetch(path, o);
  if (!r.ok) {
    let msg = r.statusText;
    try { msg = (await r.json()).detail || msg; } catch (_) {}
    throw new Error(msg);
  }
  return r.json();
}

const run = (fn) => async (...a) => { try { await fn(...a); } catch (e) { toast("Ошибка: " + e.message, 6000); } };

// ---------- вкладки ----------
let currentTab = "dash";
$$("nav button").forEach((b) => b.addEventListener("click", () => {
  $$("nav button").forEach((x) => x.classList.toggle("active", x === b));
  $$(".tab").forEach((x) => x.classList.toggle("active", x.id === "tab-" + b.dataset.tab));
  currentTab = b.dataset.tab;
  try { localStorage.setItem("tab", currentTab); } catch (_) {}
  refreshTab();
}));

function refreshTab() {
  if (currentTab === "chats") loadChats();
  if (currentTab === "accounts") loadGlobalProxy(true).then(loadAccounts);
  if (currentTab === "search") loadQueries();
  if (currentTab === "settings") loadSettings();
}

// ---------- обзор ----------
let logSince = 0;
async function refreshStatus() {
  const s = await api("/api/status");
  $("#runState").textContent = s.stopping ? "останавливается…" : s.running ? "работает" : "остановлено";
  $("#runState").classList.toggle("on", s.running);
  $("#btnStart").hidden = s.running;
  $("#btnStop").hidden = !s.running;

  const c = s.chats;
  const cards = [
    ["Всего чатов", c.total], ["В очереди", c.pending], ["Проверено", c.checked],
    [s.llm.pause > 0 ? `Ждут ИИ (пауза ${s.llm.pause} c)` : "Ждут ИИ", c.llm_queue],
    ["Живых", c.live, "live"], ["Целевых", c.target, "target"], ["Целевых живых", c.target_live, "target"],
    ["Слабых", c.low], ["Мёртвых", c.dead], ["Спам/боты", c.spam, "spam"],
    ["Нет доступа / не найдено", (c.private || 0) + (c.not_found || 0)], ["Ошибок", c.error],
  ];
  const accActive = (s.accounts.active || 0) + (s.accounts.new || 0);
  cards.push(["Аккаунтов готово", accActive], ["В FloodWait", s.accounts.flood || 0],
    ["Выбыло аккаунтов", (s.accounts.dead || 0) + (s.accounts.frozen || 0)],
    ["Потрачено на ИИ", "$" + Number(s.llm.spent || 0).toFixed(3)]);
  $("#statCards").innerHTML = cards.map(([l, v, cls]) =>
    `<div class="card ${cls || ""}"><div class="v">${typeof v === "string" ? esc(v) : fmt(v || 0)}</div><div class="l">${l}</div></div>`).join("");

  $("#workers").innerHTML = s.workers.length
    ? s.workers.map((w) => `<div class="worker"><span class="n">${esc(w.name)}</span>
        <span class="st-${w.state}">${ACC_STATUS[w.state] || w.state}</span>
        <span class="cur">${esc(w.current || "")}</span></div>`).join("")
    : `<div class="muted">Процесс не запущен. Добавьте аккаунты и чаты, затем нажмите «Запустить».</div>`;

  const items = await api("/api/log?since=" + logSince);
  if (items.length) {
    logSince = items[items.length - 1].t;
    const box = $("#log");
    const atBottom = box.scrollTop + box.clientHeight >= box.scrollHeight - 20;
    box.insertAdjacentHTML("beforeend", items.map((x) =>
      `<div class="${x.level}">${new Date(x.t * 1000).toLocaleTimeString("ru-RU")} ${esc(x.msg)}</div>`).join(""));
    while (box.childElementCount > 400) box.firstElementChild.remove();
    if (atBottom) box.scrollTop = box.scrollHeight;
  }
}

$("#btnStart").addEventListener("click", run(async () => { await api("/api/start", { json: {} }); refreshStatus(); }));
$("#btnStop").addEventListener("click", run(async () => {
  $("#btnStop").disabled = true;
  try { await api("/api/stop", { json: {} }); } finally { $("#btnStop").disabled = false; }
  refreshStatus();
}));

// ---------- чаты ----------
function chatFilters() {
  const f = {
    search: $("#fSearch").value.trim(), verdict: $("#fVerdict").value, category: $("#fCategory").value,
    type: $("#fType").value, status: $("#fStatus").value, min_members: $("#fMinMembers").value,
    target: $("#fTarget").checked ? "1" : "",
  };
  return Object.fromEntries(Object.entries(f).filter(([, v]) => v));
}

async function loadCategories() {
  const cats = await api("/api/categories");
  const sel = $("#fCategory"), cur = sel.value;
  sel.innerHTML = `<option value="">Все категории</option>` + cats.map((c) => `<option>${esc(c)}</option>`).join("");
  sel.value = cur;
}

async function loadChats() {
  const qs = new URLSearchParams({ ...chatFilters(), page, size: PAGE_SIZE, sort: $("#fSort").value });
  const d = await api("/api/chats?" + qs);
  const pages = Math.max(1, Math.ceil(d.total / PAGE_SIZE));
  if (page > pages) { page = pages; return loadChats(); }
  $("#pageInfo").textContent = `${page} / ${pages}`;
  $("#chatsInfo").textContent = `Найдено: ${fmt(d.total)}`;
  $("#chatsTable tbody").innerHTML = d.rows.map((r) => {
    const v = r.verdict ? `<span class="badge v-${r.verdict}" title="${esc(r.reason || "")}">${VERDICT[r.verdict]}</span>` : "";
    const live = r.live_score ?? r.h_live;
    let st = STATUS[r.status] || r.status;
    if (r.status === "checked" && (r.llm_status === "pending" || r.llm_status === "running")) st = "ждёт ИИ";
    const err = r.error || r.llm_error;
    return `<tr>
      <td><a href="${esc(r.link)}" target="_blank" rel="noopener">${esc(r.title || r.value)}</a>
        ${r.is_target ? ' <span class="tgt" title="Целевая категория">●</span>' : ""}
        <div class="sub">${esc(r.link.replace("https://", ""))}${r.source && r.source !== "import" ? " · " + esc(r.source) : ""}</div></td>
      <td>${r.type === "channel" ? "канал" : r.type === "group" ? "группа" : ""}</td>
      <td class="num">${fmt(r.members)}</td>
      <td class="num">${r.msgs_per_day != null ? Number(r.msgs_per_day).toFixed(1) : "—"}</td>
      <td class="num">${fmt(r.authors)}</td>
      <td class="num">${live ?? "—"}</td>
      <td class="num">${r.spam_score ?? "—"}</td>
      <td>${v}</td>
      <td>${esc(r.category || "")}<div class="sub" title="${esc(r.summary || r.about || "")}">${esc(r.topic || "")}</div></td>
      <td>${esc(st)}${err ? `<div class="sub danger-text" title="${esc(err)}">${esc(err.slice(0, 60))}</div>` : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="10" class="muted">Пусто</td></tr>`;
}

let filterTimer;
["#fSearch", "#fMinMembers"].forEach((s) => $(s).addEventListener("input", () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => { page = 1; run(loadChats)(); }, 350);
}));
["#fVerdict", "#fCategory", "#fType", "#fStatus", "#fTarget", "#fSort"].forEach((s) =>
  $(s).addEventListener("change", () => { page = 1; run(loadChats)(); }));
$("#prevPage").addEventListener("click", () => { if (page > 1) { page--; run(loadChats)(); } });
$("#nextPage").addEventListener("click", () => { page++; run(loadChats)(); });

$("#btnImportText").addEventListener("click", run(async () => {
  const r = await api("/api/chats/import", { json: { text: $("#chatsText").value } });
  $("#importResult").textContent = `Распознано ${fmt(r.recognized)}, новых ${fmt(r.added)}, не распознано ${fmt(r.bad)}`;
  $("#chatsText").value = "";
  loadChats();
}));
$("#chatsFile").addEventListener("change", run(async (e) => {
  const fd = new FormData();
  [...e.target.files].forEach((f) => fd.append("files", f));
  const r = await api("/api/chats/upload", { method: "POST", body: fd });
  $("#importResult").textContent = `Распознано ${fmt(r.recognized)}, новых ${fmt(r.added)}, не распознано ${fmt(r.bad)}`;
  e.target.value = "";
  loadChats();
}));
$("#btnExport").addEventListener("click", () => {
  window.location = "/api/export?" + new URLSearchParams(chatFilters());
});
$$("[data-retry]").forEach((b) => b.addEventListener("click", run(async () => {
  const r = await api("/api/chats/retry", { json: { what: b.dataset.retry } });
  toast(`Возвращено в очередь: ${r.n}`);
  loadChats();
})));
$$("[data-clear]").forEach((b) => b.addEventListener("click", run(async () => {
  const msg = b.dataset.clear === "all" ? "Удалить ВСЕ чаты и результаты проверок?" : "Удалить непроверенные чаты из очереди?";
  if (!confirm(msg)) return;
  const r = await api("/api/chats/clear", { json: { what: b.dataset.clear } });
  toast(`Удалено: ${r.n}`);
  loadChats();
})));

// ---------- аккаунты ----------
let globalProxy = { proxy: "", all: true, check: null };

function globalCheckText(c) {
  if (!c) return "не проверен";
  return c.ok ? `✓ работает: ${esc(c.type)}, ${c.ms} мс` : `✗ не работает: ${esc(c.error)}`;
}
function globalProxyCell() {
  const c = globalProxy.check;
  return `общий<div class="sub ${c ? (c.ok ? "ok-text" : "danger-text") : ""}">${globalCheckText(c)}</div>`;
}
function renderGlobalProxy() {
  const c = globalProxy.check;
  $("#globalProxyResult").innerHTML = globalProxy.proxy
    ? `<span class="${c ? (c.ok ? "ok-text" : "danger-text") : "muted"}">${globalCheckText(c)}</span>`
    : '<span class="muted">Общий прокси не задан</span>';
}
async function loadGlobalProxy(fillInput = false) {
  globalProxy = await api("/api/proxy/global");
  if (fillInput && document.activeElement !== $("#globalProxy")) {
    $("#globalProxy").value = globalProxy.proxy;
    $("#globalProxyAll").checked = globalProxy.all;
  }
  renderGlobalProxy();
}

$("#btnGlobalProxy").addEventListener("click", run(async () => {
  const btn = $("#btnGlobalProxy");
  btn.disabled = true;
  $("#globalProxyResult").textContent = "Проверяю…";
  try {
    const r = await api("/api/proxy/global", { json: { proxy: $("#globalProxy").value, all: $("#globalProxyAll").checked } });
    $("#globalProxy").value = r.proxy;   // с дописанным типом, если он определился
  } finally { btn.disabled = false; }
  await loadGlobalProxy();
  loadAccounts();
}));

async function loadAccounts() {
  if (!settings) settings = await api("/api/settings");
  const rows = await api("/api/accounts");
  $("#accTable tbody").innerHTML = rows.map((a) => {
    const st = a.worker || a.status;
    let extra = "";
    if (a.status === "flood" && a.flood_until * 1000 > Date.now())
      extra = " до " + new Date(a.flood_until * 1000).toLocaleTimeString("ru-RU");
    return `<tr data-id="${a.id}">
      <td><b>${esc(a.name)}</b><div class="sub">${esc(a.me || "")}</div></td>
      <td>${a.uses_global ? globalProxyCell() : a.proxy_label ? esc(a.proxy_label) : '<span class="danger-text">нет</span>'}
        ${a.uses_global ? "" : '<button class="ghost small" data-act="proxy">изменить</button>'}
        ${a.proxy_label ? `<div class="sub ${a.proxy_ok === 1 ? "ok-text" : a.proxy_ok === 0 ? "danger-text" : ""}">${
          a.proxy_ok === 1 ? "✓ " + esc(a.proxy_info) : a.proxy_ok === 0 ? "✗ " + esc(a.proxy_info) : "не проверен"}</div>` : ""}</td>
      <td class="st-${st}">${ACC_STATUS[st] || st}${extra}</td>
      <td class="num">${a.resolves_today} / ${settings.resolve_limit}</td>
      <td class="num">${a.searches_today} / ${settings.search_limit}</td>
      <td class="num">${fmt(a.checked_total)}</td>
      <td class="sub">${esc(a.last_error || "")}</td>
      <td><button class="ghost small" data-act="${a.status === "disabled" ? "enable" : "disable"}">${a.status === "disabled" ? "включить" : "откл."}</button>
        ${["dead", "frozen", "proxy_error", "flood"].includes(a.status) ? '<button class="ghost small" data-act="reset">сбросить</button>' : ""}
        <button class="ghost small danger-text" data-act="delete">удалить</button></td>
    </tr>`;
  }).join("") || `<tr><td colspan="8" class="muted">Аккаунтов нет</td></tr>`;
}

$("#accTable").addEventListener("click", run(async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const id = b.closest("tr").dataset.id;
  const act = b.dataset.act;
  if (act === "proxy") {
    const p = prompt("Прокси для аккаунта (пусто — убрать):", "");
    if (p === null) return;
    await api(`/api/accounts/${id}`, { json: { proxy: p } });
  } else if (act === "disable") await api(`/api/accounts/${id}`, { json: { status: "disabled" } });
  else if (act === "enable" || act === "reset") await api(`/api/accounts/${id}`, { json: { status: "new" } });
  else if (act === "delete") {
    if (!confirm("Удалить аккаунт и его файл сессии из папки программы?")) return;
    await api(`/api/accounts/${id}`, { method: "DELETE" });
  }
  loadAccounts();
}));

$("#accFiles").addEventListener("change", run(async (e) => {
  const fd = new FormData();
  [...e.target.files].forEach((f) => fd.append("files", f));
  const r = await api("/api/accounts/upload", { method: "POST", body: fd });
  $("#accImportResult").innerHTML = `Добавлено: ${r.added.length}, обновлено: ${r.updated.length}` +
    (r.errors.length ? `<div class="danger-text">${r.errors.map(esc).join("<br>")}</div>` : "");
  e.target.value = "";
  loadAccounts();
}));
$("#btnCheckProxyList").addEventListener("click", run(async () => {
  const btn = $("#btnCheckProxyList");
  btn.disabled = true;
  $("#proxyResult").textContent = "Проверяю… (до 25 секунд на нерабочий прокси)";
  try {
    const res = await api("/api/proxies/check", { json: { text: $("#proxyText").value } });
    const ok = res.filter((r) => r.ok), bad = res.filter((r) => !r.ok);
    // В поле оставляем только рабочие, с определённым типом — их и раздавать
    $("#proxyText").value = ok.map((r) => r.fixed).join("\n");
    $("#proxyResult").innerHTML = `<div class="ok-text">Рабочих: ${ok.length} — оставлены в поле, тип дописан.</div>` +
      (bad.length ? `<div class="danger-text">Нерабочих: ${bad.length}:<br>${bad.map((r) =>
        `${esc(r.line)} — ${esc(r.error)}`).join("<br>")}</div>` : "");
  } finally { btn.disabled = false; }
}));
$("#btnCheckProxies").addEventListener("click", run(async () => {
  const btn = $("#btnCheckProxies");
  btn.disabled = true;
  $("#netResult").textContent = "Проверяю прокси…";
  try {
    let text = "";
    if (globalProxy.proxy) {
      const g = await api("/api/proxy/global", { json: { proxy: globalProxy.proxy, all: globalProxy.all } });
      await loadGlobalProxy();
      text += `Общий прокси: <span class="${g.check.ok ? "ok-text" : "danger-text"}">${globalCheckText(g.check)}</span>. `;
    }
    const r = await api("/api/accounts/check_proxies", { json: {} });
    if (r.ok + r.bad)
      text += `Отдельные прокси: рабочих <span class="ok-text">${r.ok}</span>, нерабочих <span class="danger-text">${r.bad}</span>`;
    $("#netResult").innerHTML = text || "Прокси не заданы.";
  } finally { btn.disabled = false; }
  loadAccounts();
}));
$("#btnDirect").addEventListener("click", run(async () => {
  $("#netResult").textContent = "Проверяю прямую связь с Telegram…";
  const r = await api("/api/net/direct");
  $("#netResult").innerHTML = r.ok
    ? `<span class="ok-text">Telegram доступен с этого компьютера напрямую (${r.ms} мс).</span>`
    : `<span class="danger-text">Напрямую Telegram недоступен: ${esc(r.error)}.</span> Без рабочих прокси аккаунты не подключатся.`;
}));
$("#btnProxies").addEventListener("click", run(async () => {
  const r = await api("/api/accounts/proxies", { json: { text: $("#proxyText").value, mode: $("#proxyMode").value } });
  $("#proxyResult").innerHTML = `Назначено: ${r.assigned} из ${r.valid} подходящих. Аккаунтов осталось без прокси: ${r.without_proxy}` +
    (r.bad.length ? `<div class="danger-text">${r.bad.map(esc).join("<br>")}</div>` : "");
  loadAccounts();
}));
$("#btnCheckAcc").addEventListener("click", run(async () => {
  await api("/api/accounts/check", { json: {} });
  toast("Проверка запущена — статусы обновятся через несколько секунд");
  setTimeout(() => run(loadAccounts)(), 4000);
  setTimeout(() => run(loadAccounts)(), 12000);
}));

// ---------- поиск ----------
async function loadQueries() {
  const rows = await api("/api/queries");
  const st = { pending: "в очереди", running: "ищется", done: "готово", error: "ошибка" };
  $("#qTable tbody").innerHTML = rows.map((q) => `<tr><td>${esc(q.q)}</td><td>${st[q.status] || q.status}</td>
    <td class="num">${q.status === "done" ? fmt(q.found) : ""}</td><td class="sub">${esc(q.error || "")}</td></tr>`).join("")
    || `<tr><td colspan="4" class="muted">Запросов нет</td></tr>`;
  if (!$("#genTopics").value) {
    if (!settings) settings = await api("/api/settings");
    $("#genTopics").value = settings.target_description;
  }
}
$("#btnAddQueries").addEventListener("click", run(async () => {
  const r = await api("/api/queries", { json: { text: $("#queriesText").value } });
  $("#qResult").textContent = `Добавлено ${r.added}`;
  $("#queriesText").value = "";
  loadQueries();
}));
$("#btnGen").addEventListener("click", run(async () => {
  $("#btnGen").disabled = true;
  $("#genResult").textContent = "генерирую…";
  try {
    const r = await api("/api/queries/generate", { json: { topics: $("#genTopics").value, cities: $("#genCities").value, n: $("#genN").value } });
    const cur = $("#queriesText").value.trim();
    $("#queriesText").value = (cur ? cur + "\n" : "") + r.queries.join("\n");
    $("#genResult").textContent = `Готово: ${r.queries.length}. Проверьте список слева и нажмите «Добавить запросы».`;
  } finally { $("#btnGen").disabled = false; }
}));
$$("[data-qclear]").forEach((b) => b.addEventListener("click", run(async () => {
  if (b.dataset.qclear === "all" && !confirm("Очистить все запросы?")) return;
  await api("/api/queries/clear", { json: { what: b.dataset.qclear } });
  loadQueries();
})));

// ---------- настройки ----------
async function loadSettings() {
  settings = await api("/api/settings");
  const form = $("#settingsForm");
  for (const el of form.elements) {
    if (!el.name || !(el.name in settings)) continue;
    const v = settings[el.name];
    if (el.type === "checkbox") el.checked = !!v;
    else if (el.name === "categories") el.value = v.join("\n");
    else if (el.name === "fallback_models") el.value = v.join(", ");
    else if (el.name !== "openrouter_key") el.value = v;
  }
  $("#keyState").textContent = settings.openrouter_key_set ? "Ключ сохранён (введите новый, чтобы заменить)" : "Ключ не задан";
  $("#spent").textContent = `Потрачено всего: $${Number(settings.llm_spent || 0).toFixed(4)}`;
  renderTargetCats(settings.categories, settings.target_categories);
}

function renderTargetCats(cats, selected) {
  $("#targetCats").innerHTML = cats.map((c) =>
    `<label class="chk"><input type="checkbox" value="${esc(c)}" ${selected.includes(c) ? "checked" : ""}> ${esc(c)}</label>`).join("");
}
$('[name="categories"]').addEventListener("input", (e) => {
  const sel = $$("#targetCats input:checked").map((x) => x.value);
  renderTargetCats(e.target.value.split("\n").map((s) => s.trim()).filter(Boolean), sel);
});

$("#settingsForm").addEventListener("submit", run(async (e) => {
  e.preventDefault();
  const data = {};
  for (const el of e.target.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") data[el.name] = el.checked;
    else if (el.name === "categories") data[el.name] = el.value.split("\n").map((s) => s.trim()).filter(Boolean);
    else if (el.name === "fallback_models") data[el.name] = el.value.split(/[,\n]/).map((s) => s.trim()).filter(Boolean).slice(0, 2);
    else data[el.name] = el.value;
  }
  data.target_categories = $$("#targetCats input:checked").map((x) => x.value);
  if (!data.categories.includes("Другое")) data.categories.push("Другое");
  await api("/api/settings", { json: data });
  $("#saveResult").textContent = "Сохранено " + new Date().toLocaleTimeString("ru-RU");
  $('[name="openrouter_key"]').value = "";
  loadSettings();
}));

let models = [];
$("#btnModels").addEventListener("click", run(async () => {
  $("#btnModels").disabled = true;
  try { models = await api("/api/models"); } finally { $("#btnModels").disabled = false; }
  $("#modelList").innerHTML = models.map((m) => `<option value="${esc(m.id)}">${esc(m.name)} — $${m.in}/$${m.out} за 1M</option>`).join("");
  toast(`Загружено моделей: ${models.length}. Начните вводить название в поле «Модель».`);
  showModelInfo();
}));
function showModelInfo() {
  const m = models.find((x) => x.id === $('[name="model"]').value);
  $("#modelInfo").textContent = m ? `Цена: $${m.in} вход / $${m.out} выход за 1M токенов. ~$${((m.in * 3500 + m.out * 250) / 1e6 * 1000).toFixed(2)} за 1000 чатов` : "";
}
$('[name="model"]').addEventListener("input", showModelInfo);

$("#btnTestLlm").addEventListener("click", run(async () => {
  $("#btnTestLlm").disabled = true;
  const pre = $("#llmTestResult");
  pre.hidden = false;
  pre.textContent = "Отправляю тестовый чат… (сначала сохраните настройки, если меняли ключ или модель)";
  try {
    const r = await api("/api/llm/test", { json: {} });
    pre.textContent = JSON.stringify(r, null, 2);
  } catch (e) { pre.textContent = "Ошибка: " + e.message; }
  finally { $("#btnTestLlm").disabled = false; }
}));

// ---------- старт ----------
try { const t = localStorage.getItem("tab"); if (t) $(`nav button[data-tab="${t}"]`)?.click(); } catch (_) {}
run(refreshStatus)();
run(loadCategories)();
setInterval(() => {
  run(refreshStatus)();
  if (document.hidden) return;
  if (currentTab === "chats" && !$("#chatsTable").matches(":hover")) run(loadChats)();
  if (currentTab === "accounts") run(loadAccounts)();
  if (currentTab === "search") run(loadQueries)();
}, 3000);
setInterval(() => run(loadCategories)(), 30000);
