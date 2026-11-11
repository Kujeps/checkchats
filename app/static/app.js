const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const fmt = (n) => (n == null ? "—" : Number(n).toLocaleString("ru-RU"));

const COUNTRY = { RU: "Россия", UA: "Украина", KZ: "Казахстан", BY: "Беларусь", UZ: "Узбекистан", KG: "Киргизия",
  TJ: "Таджикистан", AM: "Армения", GE: "Грузия", AZ: "Азербайджан", MD: "Молдова", "?": "—" };
const VERDICT = { live: "Живой", low: "Слабый", dead: "Мёртвый", spam: "Спам/боты", noconv: "Нет общения", private: "Закрытый" };
const STATUS = { pending: "в очереди", checking: "проверяется", checked: "проверен", not_found: "не найден",
  not_chat: "не чат", private: "нет доступа", error: "ошибка", duplicate: "дубль" };
const ACC_STATUS = { new: "новый", active: "активен", flood: "FloodWait", dead: "мёртв", frozen: "заморожен",
  proxy_error: "нет связи", disabled: "отключён", working: "работает", idle: "ждёт задач", limit: "лимит на сегодня",
  connecting: "подключение", queue: "в очереди", paused: "режимы выключены", search: "ищет чаты", starting: "старт", stopped: "остановлен" };

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
  if (currentTab === "accounts") { loadGlobalProxy(true).then(loadAccounts); loadAccountTags(); }
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
  for (const [id, key] of [["#modeSearch", "search"], ["#modeCheck", "check"]])
    if (document.activeElement !== $(id)) $(id).checked = s.modes[key];
  $("#btnStop").hidden = !s.running;

  const c = s.chats;
  const cards = [
    ["Всего чатов", c.total], ["В очереди", c.pending], ["Проверено", c.checked],
    [s.llm.pause > 0 ? `Ждут ИИ (пауза ${s.llm.pause} c)` : "Ждут ИИ", c.llm_queue],
    ["Живых", c.live, "live"], ["Целевых", c.target, "target"], ["Целевых живых", c.target_live, "target"],
    ["Слабых", c.low], ["Мёртвых", c.dead], ["Спам/боты", c.spam, "spam"], ["Нет общения", c.noconv],
    ["Нет доступа / не найдено", (c.private || 0) + (c.not_found || 0)], ["Ошибок", c.error],
  ];
  const accActive = (s.accounts.active || 0) + (s.accounts.new || 0);
  cards.push(["Аккаунтов готово", accActive], ["В FloodWait", s.accounts.flood || 0],
    ["Выбыло аккаунтов", (s.accounts.dead || 0) + (s.accounts.frozen || 0)],
    ["Потрачено на ИИ", "$" + Number(s.llm.spent || 0).toFixed(3)]);
  $("#statCards").innerHTML = cards.map(([l, v, cls]) =>
    `<div class="card ${cls || ""}"><div class="v">${typeof v === "string" ? esc(v) : fmt(v || 0)}</div><div class="l">${l}</div></div>`).join("");

  const sl = s.slots || {};
  $("#slotsInfo").textContent = s.running
    ? `— работают ${sl.active}${sl.limit ? " из " + sl.limit : ""}, в очереди ${sl.waiting}` : "";
  // Сначала те, кто занят делом, потом ждущие и отдыхающие
  const order = { working: 0, search: 0, idle: 1, connecting: 1, queue: 2, flood: 3, limit: 3 };
  const workers = [...s.workers].sort((a, b) => (order[a.state] ?? 4) - (order[b.state] ?? 4));
  $("#workers").innerHTML = workers.length
    ? workers.map((w) => `<div class="worker"><span class="n">${esc(w.name)}</span>
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

$("#btnStart").addEventListener("click", run(openStartDialog));
// Режимы можно переключать и на ходу — аккаунты подхватят их в течение нескольких секунд
for (const [id, key] of [["#modeSearch", "run_search"], ["#modeCheck", "run_check"]])
  $(id).addEventListener("change", run(async (e) => {
    await api("/api/settings", { json: { [key]: e.target.checked } });
    toast(e.target.checked ? `${key === "run_search" ? "Поиск" : "Проверка"} включен${key === "run_search" ? "" : "а"}`
                           : `${key === "run_search" ? "Поиск" : "Проверка"} выключен${key === "run_search" ? "" : "а"}`);
  }));
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
    country: $("#fCountry").value, city: $("#fCity").value.trim(),
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
      <td>${r.country ? esc(COUNTRY[r.country] || r.country) : ""}${r.city ? `<div class="sub">${esc(r.city)}</div>` : ""}</td>
      <td class="num">${fmt(r.members)}</td>
      <td class="num">${r.msgs_per_day != null ? Number(r.msgs_per_day).toFixed(1) : "—"}</td>
      <td class="num">${fmt(r.weekly_active ?? r.authors)}${r.human != null ? `<div class="sub">${Math.round(r.human * 100)}% от людей</div>` : ""}</td>
      <td class="num">${live ?? "—"}</td>
      <td class="num">${r.spam_score ?? "—"}</td>
      <td>${v}</td>
      <td>${esc(r.category || "")}<div class="sub" title="${esc(r.summary || r.about || "")}">${esc(r.topic || "")}</div></td>
      <td>${esc(st)}${err ? `<div class="sub danger-text" title="${esc(err)}">${esc(err.slice(0, 60))}</div>` : ""}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="11" class="muted">Пусто</td></tr>`;
}

let filterTimer;
["#fSearch", "#fMinMembers", "#fCity"].forEach((s) => $(s).addEventListener("input", () => {
  clearTimeout(filterTimer);
  filterTimer = setTimeout(() => { page = 1; run(loadChats)(); }, 350);
}));
["#fVerdict", "#fCategory", "#fType", "#fCountry", "#fStatus", "#fTarget", "#fSort"].forEach((s) =>
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
  if (b.dataset.retry === "recheck" && !confirm(
    "Заново прочитать все проверенные чаты и оценить по новым правилам?\n" +
    "Подходящие чаты снова пойдут в ИИ (это стоит денег). Перечитываться будут тем же аккаунтом, " +
    "лимит поиска по username при этом почти не тратится.")) return;
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

// ---------- менеджер аккаунтов ----------
let accRows = [];
const accSelected = new Set();
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" });

// Состояние аккаунта простыми словами: что с ним и может ли он работать
function accState(a) {
  const floods = [];
  if (a.flood_until) floods.push(`FloodWait на всё до ${hhmm(a.flood_until)}`);
  if (a.flood_resolve_until) floods.push(`FloodWait на поиск по username до ${hhmm(a.flood_resolve_until)} — искать чаты может`);
  if (a.flood_search_until) floods.push(`FloodWait на поиск чатов до ${hhmm(a.flood_search_until)} — проверять может`);
  const bad = ["dead", "frozen", "proxy_error"].includes(a.status);
  const st = a.worker || (a.status === "flood" && !a.flood_until ? "active" : a.status);
  return {
    key: st, text: ACC_STATUS[st] || st, floods, bad,
    ready: !bad && !a.flood_until,
    lines: [...floods, ...(a.last_error && !a.last_error.startsWith("FloodWait") ? [a.last_error] : [])],
  };
}

function accMatches(a) {
  const q = $("#accSearch").value.trim().toLowerCase();
  if (q && ![a.name, a.phone, a.me, a.note, a.tag].some((x) => (x || "").toLowerCase().includes(q))) return false;
  const tag = $("#accTagFilter").value;
  if (tag && a.tag !== tag) return false;
  const st = accState(a);
  switch ($("#accFilter").value) {
    case "work": return !!a.enabled;
    case "off": return !a.enabled;
    case "ready": return st.ready;
    case "flood": return st.floods.length > 0;
    case "problem": return st.bad;
  }
  return true;
}

async function loadAccountTags() {
  const tags = await api("/api/accounts/tags");
  for (const id of ["#accTagFilter", "#smTag"]) {
    const sel = $(id), cur = sel.value;
    sel.innerHTML = `<option value="">${id === "#smTag" ? "Выбрать группу…" : "Все группы"}</option>` +
      tags.map((x) => `<option>${esc(x)}</option>`).join("");
    sel.value = cur;
  }
}

async function loadAccounts() {
  if (!settings) settings = await api("/api/settings");
  accRows = await api("/api/accounts");
  const ids = new Set(accRows.map((a) => a.id));
  for (const id of [...accSelected]) if (!ids.has(id)) accSelected.delete(id);
  const shown = accRows.filter(accMatches);
  const inWork = accRows.filter((a) => a.enabled).length;
  $("#accSummary").textContent = `— всего ${accRows.length}, в работе ${inWork}, готовы ${accRows.filter((a) => accState(a).ready).length}`;
  $("#accTable tbody").innerHTML = shown.map((a) => {
    const st = accState(a);
    const phone = a.phone && a.phone !== a.name ? `+${esc(a.phone)}` : "";
    return `<tr data-id="${a.id}" class="${accSelected.has(a.id) ? "sel" : ""}">
      <td><input type="checkbox" class="acc-sel" ${accSelected.has(a.id) ? "checked" : ""}></td>
      <td><b>${esc(a.name)}</b> ${phone}<div class="sub">${esc(a.me || "не проверялся")}${a.twofa ? " · 2FA" : ""}</div></td>
      <td>${a.tag ? `<span class="tagb">${esc(a.tag)}</span>` : ""}
        <button class="linkish" data-act="tag">${a.tag ? "изменить" : "+ группа"}</button>
        <div class="sub">${esc(a.note || "")} <button class="linkish" data-act="note">${a.note ? "изменить" : "+ заметка"}</button></div></td>
      <td><label class="chk"><input type="checkbox" data-act="toggle" ${a.enabled ? "checked" : ""}></label></td>
      <td>${a.uses_global ? globalProxyCell() : a.proxy_label ? esc(a.proxy_label) : '<span class="danger-text">нет</span>'}
        ${a.uses_global ? "" : '<button class="linkish" data-act="proxy">изменить</button>'}
        ${a.proxy_label ? `<div class="sub ${a.proxy_ok === 1 ? "ok-text" : a.proxy_ok === 0 ? "danger-text" : ""}">${
          a.proxy_ok === 1 ? "✓ " + esc(a.proxy_info) : a.proxy_ok === 0 ? "✗ " + esc(a.proxy_info) : "не проверен"}</div>` : ""}</td>
      <td><span class="st-${st.key}">${esc(st.text)}</span>${st.lines.map((l) => `<div class="sub">${esc(l)}</div>`).join("")}</td>
      <td class="num">${a.resolves_today} / ${settings.resolve_limit}</td>
      <td class="num">${a.searches_today} / ${settings.search_limit}</td>
      <td class="num">${fmt(a.checked_total)}</td>
    </tr>`;
  }).join("") || `<tr><td colspan="9" class="muted">${accRows.length ? "Ничего не подходит под фильтр" : "Аккаунтов нет"}</td></tr>`;
  $("#accSelAll").checked = shown.length > 0 && shown.every((a) => accSelected.has(a.id));
  updateBulk();
}

function updateBulk() {
  const n = accSelected.size;
  $("#accSelInfo").textContent = n ? `Выбрано: ${n}` : "Отметьте аккаунты галочками, чтобы сделать что-то сразу со всеми";
  $$("[data-bulk]").forEach((b) => (b.disabled = !n));
}

["#accSearch"].forEach((s) => $(s).addEventListener("input", () => run(loadAccounts)()));
["#accFilter", "#accTagFilter"].forEach((s) => $(s).addEventListener("change", () => run(loadAccounts)()));

$("#accSelAll").addEventListener("change", (e) => {
  for (const a of accRows.filter(accMatches)) e.target.checked ? accSelected.add(a.id) : accSelected.delete(a.id);
  run(loadAccounts)();
});

async function bulk(action, ids, value) {
  return api("/api/accounts/bulk", { json: { ids, action, value } });
}

$("#accTable").addEventListener("change", run(async (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (!tr) return;
  const id = +tr.dataset.id;
  if (e.target.classList.contains("acc-sel")) {
    e.target.checked ? accSelected.add(id) : accSelected.delete(id);
    tr.classList.toggle("sel", e.target.checked);
    updateBulk();
  } else if (e.target.dataset.act === "toggle") {
    await bulk(e.target.checked ? "enable" : "disable", [id]);
    loadAccounts();
  }
}));

$("#accTable").addEventListener("click", run(async (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  const id = +b.closest("tr").dataset.id;
  const a = accRows.find((x) => x.id === id);
  const act = b.dataset.act;
  if (act === "proxy") {
    const p = prompt("Прокси для аккаунта (пусто — убрать):", "");
    if (p === null) return;
    await api(`/api/accounts/${id}`, { json: { proxy: p } });
  } else if (act === "tag" || act === "note") {
    const v = prompt(act === "tag" ? "Группа аккаунта (пусто — убрать):" : "Заметка (пусто — убрать):", a[act] || "");
    if (v === null) return;
    await bulk(act, [id], v);
    if (act === "tag") loadAccountTags();
  }
  loadAccounts();
}));

$$("[data-bulk]").forEach((b) => b.addEventListener("click", run(async () => {
  const ids = [...accSelected], n = ids.length, act = b.dataset.bulk;
  let value;
  if (act === "tag" || act === "note") {
    value = prompt(act === "tag" ? `Группа для ${n} аккаунтов (пусто — убрать):` : `Заметка для ${n} аккаунтов (пусто — убрать):`, "");
    if (value === null) return;
  }
  if (act === "delete" && !confirm(`Удалить ${n} аккаунтов вместе с файлами сессий из папки программы? Это нельзя отменить.`)) return;
  if (act === "reset" && !confirm(`Сбросить FloodWait и ошибки у ${n} аккаунтов? Если ограничение Telegram ещё действует, аккаунт просто поймает его снова.`)) return;
  const r = await bulk(act, ids, value);
  const done = { enable: "В работу", disable: "Убрано из работы", tag: "Группа задана", note: "Заметка задана",
                 check: "Проверка подключения запущена", reset: "Сброшено", delete: "Удалено" }[act];
  toast(`${done}: ${r.n}${r.busy ? ` (${r.busy} сейчас работают — пропущены)` : ""}`);
  if (act === "delete") accSelected.clear();
  if (act === "tag") loadAccountTags();
  loadAccounts();
  if (act === "check") { setTimeout(() => run(loadAccounts)(), 5000); setTimeout(() => run(loadAccounts)(), 15000); }
})));

// ---------- окно запуска ----------
const smChosen = new Set();

function renderStartList() {
  const search = $("#smSearch").checked, check = $("#smCheck").checked;
  $("#smList").innerHTML = accRows.map((a) => {
    const st = accState(a);
    // Что аккаунт сможет делать прямо сейчас в выбранных режимах
    let note;
    if (st.bad) note = st.text;
    else if (a.flood_until) note = `FloodWait до ${hhmm(a.flood_until)}`;
    else {
      const canCheck = check && !a.flood_resolve_until, canSearch = search && !a.flood_search_until;
      if ((!check || canCheck) && (!search || canSearch)) note = "готов";
      else if (canSearch) note = `пока только поиск, проверка после ${hhmm(a.flood_resolve_until)}`;
      else if (canCheck) note = `пока только проверка, поиск после ${hhmm(a.flood_search_until)}`;
      else note = `сможет после ${hhmm(Math.min(...[check && a.flood_resolve_until, search && a.flood_search_until].filter(Boolean)))}`;
    }
    const cls = st.bad || a.flood_until || note.startsWith("сможет") ? "danger-text" : note === "готов" ? "ok-text" : "muted";
    return `<label><input type="checkbox" data-id="${a.id}" ${smChosen.has(a.id) ? "checked" : ""}>
      <span class="n">${esc(a.name)}</span>${a.tag ? `<span class="tagb">${esc(a.tag)}</span>` : ""}
      <span class="muted">${esc(a.note || "")}</span><span class="st ${cls}">${esc(note)}</span></label>`;
  }).join("") || '<div class="muted" style="padding:10px">Аккаунтов нет — добавьте их на вкладке «Аккаунты».</div>';
  $("#smInfo").textContent = ` — выбрано ${smChosen.size} из ${accRows.length}`;
  $("#smStart").textContent = `Запустить${smChosen.size ? ` (${smChosen.size})` : ""}`;
}

async function openStartDialog() {
  await Promise.all([loadAccounts(), loadAccountTags()]);
  smChosen.clear();
  for (const a of accRows) if (a.enabled && !accState(a).bad) smChosen.add(a.id);
  $("#smSearch").checked = $("#modeSearch").checked;
  $("#smCheck").checked = $("#modeCheck").checked;
  renderStartList();
  $("#startModal").hidden = false;
}

$("#smList").addEventListener("change", (e) => {
  const id = +e.target.dataset.id;
  e.target.checked ? smChosen.add(id) : smChosen.delete(id);
  renderStartList();
});
["#smSearch", "#smCheck"].forEach((s) => $(s).addEventListener("change", renderStartList));
$$("[data-sm]").forEach((b) => b.addEventListener("click", () => {
  smChosen.clear();
  for (const a of accRows) {
    if (b.dataset.sm === "all" && !accState(a).bad) smChosen.add(a.id);
    if (b.dataset.sm === "ready" && accState(a).ready) smChosen.add(a.id);
  }
  renderStartList();
}));
$("#smTag").addEventListener("change", (e) => {
  if (!e.target.value) return;
  smChosen.clear();
  for (const a of accRows) if (a.tag === e.target.value && !accState(a).bad) smChosen.add(a.id);
  e.target.value = "";
  renderStartList();
});
$("#smCancel").addEventListener("click", () => ($("#startModal").hidden = true));
$("#startModal").addEventListener("click", (e) => { if (e.target.id === "startModal") $("#startModal").hidden = true; });

$("#smStart").addEventListener("click", run(async () => {
  const search = $("#smSearch").checked, check = $("#smCheck").checked;
  if (!search && !check) return toast("Включите поиск, проверку или оба режима");
  if (!smChosen.size) return toast("Выберите хотя бы один аккаунт");
  $("#smStart").disabled = true;
  try {
    await bulk("only", [...smChosen]);
    await api("/api/settings", { json: { run_search: search, run_check: check } });
    await api("/api/start", { json: {} });
  } finally { $("#smStart").disabled = false; }
  $("#startModal").hidden = true;
  refreshStatus();
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

// ---------- поиск ----------
async function loadQueries() {
  const rows = await api("/api/queries");
  const st = { pending: "в очереди", running: "ищется", done: "готово", error: "ошибка" };
  const kinds = { both: "названия и сообщения", messages: "сообщения", title: "названия" };
  $("#qTable tbody").innerHTML = rows.map((q) => `<tr><td>${esc(q.q)}</td><td>${kinds[q.kind] || "названия"}</td><td>${st[q.status] || q.status}</td>
    <td class="num">${q.status === "done" ? fmt(q.found) : ""}</td><td class="sub">${esc(q.error || "")}</td></tr>`).join("")
    || `<tr><td colspan="5" class="muted">Запросов нет</td></tr>`;
  if (!$("#genTopics").value) {
    if (!settings) settings = await api("/api/settings");
    $("#genTopics").value = settings.target_description;
  }
}
$("#btnAddQueries").addEventListener("click", run(async () => {
  const r = await api("/api/queries", { json: { text: $("#queriesText").value, kind: $("#queryKind").value } });
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
    else if (el.name === "fallback_models" || el.name === "target_countries") el.value = v.join(", ");
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
    else if (el.name === "target_countries") data[el.name] = el.value.split(/[,\s]+/).map((s) => s.trim().toUpperCase()).filter(Boolean);
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

// ---------- обновление программы ----------
let bootId = null;
async function loadVersion() {
  const v = await api("/api/version");
  bootId = v.boot;
  $("#versionInfo").textContent = v.updatable
    ? `— версия ${v.commit} от ${new Date(v.date).toLocaleDateString("ru-RU")}`
    : "";
  if (!v.updatable) {
    $("#btnCheckUpdate").disabled = true;
    $("#updateInfo").innerHTML = `<span class="muted">Обновление кнопкой недоступно: ${esc(v.reason)}</span>`;
  }
}

$("#btnCheckUpdate").addEventListener("click", run(async () => {
  const btn = $("#btnCheckUpdate");
  btn.disabled = true;
  $("#updateInfo").textContent = "Проверяю…";
  try {
    const r = await api("/api/update/check", { json: {} });
    $("#btnUpdate").hidden = !r.behind;
    $("#updateInfo").innerHTML = r.behind
      ? `<div class="ok-text">Доступно обновлений: ${r.behind}</div><ul>${r.changes.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`
      : '<span class="muted">У вас последняя версия.</span>';
  } finally { btn.disabled = false; }
}));

$("#btnUpdate").addEventListener("click", run(async () => {
  if (!confirm("Обновить программу и перезапустить её? Работающие аккаунты аккуратно остановятся, " +
               "после перезапуска нажмите «Запустить» снова.")) return;
  $("#btnUpdate").disabled = $("#btnCheckUpdate").disabled = true;
  $("#updateInfo").textContent = "Скачиваю обновление… (если поменялись зависимости — до пары минут)";
  let r;
  try { r = await api("/api/update", { json: {} }); }
  catch (e) { $("#btnUpdate").disabled = $("#btnCheckUpdate").disabled = false; throw e; }
  if (!r.restart) {
    $("#updateInfo").innerHTML = '<span class="muted">Обновлять нечего — у вас последняя версия.</span>';
    $("#btnUpdate").hidden = true;
    $("#btnCheckUpdate").disabled = false;
    return;
  }
  $("#updateInfo").innerHTML = `<div class="ok-text">Обновлено, перезапуск…</div>`;
  // Ждём, пока старая копия выключится и новая поднимется, затем перезагружаем страницу
  const started = Date.now();
  await new Promise((res) => setTimeout(res, 2500));
  while (Date.now() - started < 120000) {
    try {
      const v = await fetch("/api/version").then((x) => x.json());
      if (v.boot && v.boot !== bootId) { location.reload(); return; }
    } catch (_) {}
    await new Promise((res) => setTimeout(res, 1500));
  }
  $("#updateInfo").innerHTML = '<span class="danger-text">Программа не поднялась сама — запустите run.bat заново.</span>';
}));

// ---------- старт ----------
try { const t = localStorage.getItem("tab"); if (t) $(`nav button[data-tab="${t}"]`)?.click(); } catch (_) {}
run(refreshStatus)();
run(loadCategories)();
run(loadVersion)();
setInterval(() => {
  run(refreshStatus)();
  if (document.hidden) return;
  if (currentTab === "chats" && !$("#chatsTable").matches(":hover")) run(loadChats)();
  // пока открыт ввод в менеджере или окно запуска — не перерисовываем таблицу под пальцами
  if (currentTab === "accounts" && $("#startModal").hidden && document.activeElement !== $("#accSearch")) run(loadAccounts)();
  if (currentTab === "search") run(loadQueries)();
}, 3000);
setInterval(() => run(loadCategories)(), 30000);
