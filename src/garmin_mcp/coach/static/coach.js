/* Trener tab: plan calendar, progress, activity analysis and coach chat.
   Uses helpers from index.html: css, ddmm, fmt, hm, sportColor, SPORT_PL, baseOpts, draw. */

const C = {
  week: 0, view: null, thread: "general", activityId: null, activity: null,
  selectedSession: null, sending: false,
};
const PLAN_SPORT_PL = { ...SPORT_PL, brick: "Zakładka", rest: "Odpoczynek" };
const STATE_ICON = { done: ["st-done", "✓", "zrobione"], missed: ["st-missed", "✕", "opuszczone"],
  skipped: ["st-skipped", "–", "pominięte"], today: ["st-today", "•", "dziś"],
  planned: ["st-planned", "", "zaplanowane"], rest: ["st-rest", "", "odpoczynek"] };
const DOW = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"];

const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const planColor = s => s === "brick" ? css("--bike") : sportColor(s);
const api = async (url, body) => {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-Coach": "1" }, body: JSON.stringify(body) };
  const r = await fetch(url, opt);
  const d = await r.json();
  if (!r.ok || d.error) throw new Error(d.error || r.statusText);
  return d;
};
const mmss = sec => { if (sec == null || !isFinite(sec)) return "—"; sec = Math.round(sec); return Math.floor(sec / 60) + ":" + String(sec % 60).padStart(2, "0"); };
const paceKm = mps => mps ? mmss(1000 / mps) + " /km" : "—";
const pace100 = mps => mps ? mmss(100 / mps) + " /100 m" : "—";
const durMin = m => m ? (m >= 60 ? `${Math.floor(m / 60)} h ${m % 60 ? Math.round(m % 60) + " min" : ""}` : `${Math.round(m)} min`) : "";
const distTxt = (sport, m) => !m ? "—" : sport === "swim" ? `${Math.round(m)} m` : `${(m / 1000).toFixed(1)} km`;

/* ---------- markdown (escaped first, then a small safe subset) ---------- */
function md(text) {
  const lines = esc(text).split("\n");
  let html = "", list = false, para = [];
  const inline = s => s.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*(?!\s)(.+?)\*/g, "$1<i>$2</i>").replace(/`([^`]+)`/g, "<code>$1</code>");
  const flush = () => { if (para.length) { html += `<p>${inline(para.join("<br>"))}</p>`; para = []; } };
  for (const raw of lines) {
    const l = raw.trimEnd();
    const li = l.match(/^\s*(?:[-*•]|\d+[.)])\s+(.*)/);
    if (li) { flush(); if (!list) { html += "<ul>"; list = true; } html += `<li>${inline(li[1])}</li>`; continue; }
    if (list) { html += "</ul>"; list = false; }
    const h = l.match(/^#{1,4}\s+(.*)/);
    if (h) { flush(); html += `<h4>${inline(h[1])}</h4>`; continue; }
    if (!l.trim()) { flush(); continue; }
    para.push(l);
  }
  flush(); if (list) html += "</ul>";
  return html;
}

/* ---------- tabs & routing ---------- */
const TAB_HASH = { dash: "#", coach: "#trener", body: "#sklad" };
function showTab(tab) {
  document.querySelectorAll(".tabs button").forEach(b => {
    const on = b.dataset.tab === tab;
    b.setAttribute("aria-selected", on);
    $("tab-" + b.dataset.tab).hidden = !on;
  });
  if (tab === "coach" && !C.view) loadTrainer();
  if (tab === "body" && window.loadBody) window.loadBody();
}
document.querySelectorAll(".tabs button").forEach(b => b.addEventListener("click", () => {
  history.replaceState(null, "", TAB_HASH[b.dataset.tab]);
  showTab(b.dataset.tab);
}));

/* ---------- trainer data ---------- */
async function loadTrainer() {
  try {
    C.view = await api(`/api/trainer?week=${C.week}`);
    renderPlan(); renderProgress(); renderSettings(false); renderGcal();
  } catch (e) {
    $("calendar").innerHTML = `<p class="err">Nie mogę pobrać danych trenera: ${esc(e.message)}</p>`;
  }
}
$("weekPrev").onclick = () => { C.week -= 1; loadTrainer(); };
$("weekNext").onclick = () => { C.week += 1; loadTrainer(); };
$("weekNow").onclick = () => { C.week = 0; loadTrainer(); };

function renderPlan() {
  const v = C.view, start = new Date(v.start + "T12:00:00");
  $("planRange").textContent = `${ddmm(v.start)} – ${ddmm(v.end)}`;
  const byDay = {};
  v.sessions.forEach(s => (byDay[s.date] ||= { s: [], x: [] }).s.push(s));
  v.unplanned.forEach(a => (byDay[a.date] ||= { s: [], x: [] }).x.push(a));
  let html = "";
  for (let i = 0; i < 14; i++) {
    const d = new Date(start); d.setDate(start.getDate() + i);
    const iso = d.toISOString().slice(0, 10), items = byDay[iso] || { s: [], x: [] };
    const cls = ["day", iso === v.today ? "today" : "", iso < v.today ? "past" : "", !items.s.length && !items.x.length ? "empty-day" : ""].join(" ");
    html += `<div class="${cls}"><div class="day-h"><b>${DOW[i % 7]}</b><span>${ddmm(iso)}</span></div>`;
    for (const s of items.s) {
      const [stc, sym, lbl] = STATE_ICON[s.state] || STATE_ICON.planned;
      const meta = [s.duration_min ? durMin(s.duration_min) : "", s.intensity || ""].filter(Boolean).join(" · ");
      html += `<button class="sess ${s.state} ${C.selectedSession === s.id ? "sel" : ""}" style="--sc:${planColor(s.sport)}" data-sid="${s.id}"
        title="${esc(PLAN_SPORT_PL[s.sport] || s.sport)} — ${lbl}"><span class="t">${esc(s.title || PLAN_SPORT_PL[s.sport])}</span>
        <span class="m"><span>${esc(meta)}</span><i class="st ${stc}" aria-label="${lbl}">${sym}</i></span></button>`;
    }
    for (const a of items.x) {
      html += `<button class="sess extra" style="--sc:${sportColor(a.sport)}" data-aid="${a.id}" title="Trening poza planem — kliknij, aby omówić">
        <span class="t">${esc(a.name || SPORT_PL[a.sport])}</span><span class="m"><span>${hm(a.duration_s)} h</span><i class="st st-extra">+</i></span></button>`;
    }
    html += "</div>";
  }
  $("calendar").innerHTML = html;
  $("planEmpty").hidden = v.sessions.length > 0;
  $("calendar").querySelectorAll("[data-sid]").forEach(b => b.onclick = () => selectSession(+b.dataset.sid));
  $("calendar").querySelectorAll("[data-aid]").forEach(b => b.onclick = () => openActivity(+b.dataset.aid));
  if (C.selectedSession) renderSessionDetail();
}

function selectSession(id) {
  C.selectedSession = C.selectedSession === id ? null : id;
  renderPlan();
  if (!C.selectedSession) $("sessionDetail").hidden = true;
}

function renderSessionDetail() {
  const s = C.view.sessions.find(x => x.id === C.selectedSession);
  const box = $("sessionDetail");
  if (!s) { box.hidden = true; return; }
  const [, , lbl] = STATE_ICON[s.state] || STATE_ICON.planned;
  box.hidden = false;
  box.innerHTML = `<h3>${esc(s.title || PLAN_SPORT_PL[s.sport])}</h3>
    <div class="kv"><span>${esc(s.weekday)} ${ddmm(s.date)}</span><span><b>${esc(PLAN_SPORT_PL[s.sport] || s.sport)}</b></span>
      ${s.duration_min ? `<span>czas <b>${durMin(s.duration_min)}</b></span>` : ""}
      ${s.distance_km ? `<span>dystans <b>${s.distance_km} km</b></span>` : ""}
      ${s.intensity ? `<span>intensywność <b>${esc(s.intensity)}</b></span>` : ""}<span>status: <b>${lbl}</b></span></div>
    <div class="desc">${esc(s.description || "Brak opisu.")}</div>
    <div class="btn-row">
      ${s.activity ? `<button class="small primary" data-act="open">Omów wykonany trening</button>` : ""}
      <button class="small" data-act="ask">Zapytaj trenera o tę sesję</button>
      ${s.status === "skipped" ? `<button class="small" data-act="unskip">Przywróć</button>` : `<button class="small" data-act="skip">Oznacz jako pominiętą</button>`}
      <button class="small ghost" data-act="del">Usuń z planu</button>
    </div>`;
  box.querySelectorAll("button").forEach(b => b.onclick = async () => {
    const act = b.dataset.act;
    if (act === "open") return openActivity(s.activity.id);
    if (act === "ask") { setThread("general"); return send(`Sesja ${s.weekday} ${ddmm(s.date)} „${s.title}”: wytłumacz jej cel i jak ją dobrze wykonać. Czy przy mojej obecnej formie zostawić ją bez zmian?`); }
    const ops = act === "del" ? { delete: [s.id] } : { upsert: [{ id: s.id, status: act === "skip" ? "skipped" : "planned" }] };
    if (act === "del") C.selectedSession = null;
    await api("/api/plan", ops).catch(e => alert(e.message));
    loadTrainer();
  });
}

/* ---------- Google Calendar ---------- */
let gcalOpen = false;
$("gcalBtn").onclick = () => { gcalOpen = !gcalOpen; renderGcal(); };
function renderGcal() {
  const g = C.view && C.view.gcal, box = $("gcalBox");
  if (!g) return;
  $("gcalBtn").classList.toggle("on", g.connected);
  $("gcalBtn").textContent = g.connected ? "📅 Kalendarz Google ✓" : "📅 Kalendarz Google";
  box.hidden = !gcalOpen && !(g.connected && g.last_error);
  if (box.hidden) return;
  if (g.connected) {
    box.innerHTML = `<h3>Kalendarz Google: połączono</h3>
      <p style="margin:0 0 8px">Treningi z planu są w kalendarzu <b>„Trening”</b> jako wydarzenia całodniowe i aktualizują się same po każdej zmianie planu.
      ${g.last_sync ? `Ostatnia synchronizacja: ${esc(g.last_sync.replace("T", " ").slice(0, 16))}. ${esc(g.last_message || "")}` : ""}</p>
      ${g.last_error ? `<p class="err">${esc(g.last_error)}</p>` : ""}
      <div class="btn-row"><button class="small primary" id="gcalSync">Synchronizuj teraz</button>
      <button class="small ghost" id="gcalOff">Odłącz</button></div>`;
    $("gcalSync").onclick = async () => {
      $("gcalSync").disabled = true;
      const r = await api("/api/gcal/sync", {}).catch(e => ({ message: e.message }));
      C.view.gcal = await api("/api/gcal"); gcalOpen = true; renderGcal();
      if (!r.ok) alert(r.message);
    };
    $("gcalOff").onclick = async () => {
      if (!confirm("Odłączyć Kalendarz Google? Kalendarz „Trening” zostanie w Google — możesz go usunąć ręcznie.")) return;
      C.view.gcal = await api("/api/gcal/disconnect", {}); renderGcal();
    };
    return;
  }
  box.innerHTML = `<h3>Treningi w Kalendarzu Google</h3>
    <p style="margin:0">Jednorazowa konfiguracja (ok. 5 min) — potrzebny jest Twój własny klucz OAuth z Google Cloud:</p>
    <ol>
      <li>Wejdź na <a href="https://console.cloud.google.com/" target="_blank" rel="noopener">console.cloud.google.com</a> i utwórz projekt (np. „Trener”).</li>
      <li><i>APIs &amp; Services → Library</i> → <b>Google Calendar API</b> → <b>Enable</b>.</li>
      <li><i>Google Auth Platform</i> (ekran zgody OAuth): typ <b>External</b>, nazwa aplikacji, Twój e-mail. W <i>Audience</i> kliknij <b>Publish app</b> („In production”) — inaczej Google wylogowuje co 7 dni.</li>
      <li><i>Clients → Create client</i> → typ <b>Desktop app</b> → skopiuj <b>Client ID</b> i <b>Client secret</b> poniżej.</li>
      <li>Kliknij „Połącz z Google”. Google ostrzeże, że aplikacja nie jest zweryfikowana — to Twoja własna aplikacja: <i>Zaawansowane → Przejdź do…</i>.</li>
    </ol>
    <label>Client ID</label><input id="gcalId" placeholder="….apps.googleusercontent.com" autocomplete="off">
    <label>Client secret</label><input id="gcalSecret" type="password" placeholder="${g.configured ? "zapisany — wpisz nowy, aby zmienić" : "GOCSPX-…"}" autocomplete="off">
    <p style="margin:0 0 8px">Aplikacja dostaje dostęp <b>tylko do kalendarza, który sama utworzy</b> („Trening”) — nie widzi Twoich innych kalendarzy.</p>
    <div class="btn-row"><button class="small" id="gcalSave">Zapisz klucz</button>
      ${g.configured ? `<a class="button primary small" href="/oauth/google/start">Połącz z Google</a>` : ""}</div>
    ${g.last_error ? `<p class="err">${esc(g.last_error)}</p>` : ""}`;
  $("gcalSave").onclick = async () => {
    try {
      C.view.gcal = await api("/api/gcal/client", { client_id: $("gcalId").value, client_secret: $("gcalSecret").value });
      gcalOpen = true; renderGcal();
    } catch (e) { alert(e.message); }
  };
}

/* ---------- progress ---------- */
function progValue(p, v) {
  if (v == null) return "—";
  if (p.sport === "run") return paceKm(v);
  if (p.sport === "bike") return `${Math.round(v)} W`;
  return mmss(v) + " /100 m";
}
function renderProgress() {
  const pr = C.view.progress;
  $("progress").innerHTML = pr.sports.map(p => {
    const arrow = { progres: "▲", regres: "▼", stabilnie: "→" }[p.verdict] || "";
    const cls = { progres: "v-progres", regres: "v-regres", stabilnie: "v-stabilnie" }[p.verdict] || "v-brak";
    const chg = p.change_pct != null ? `${p.change_pct > 0 ? "+" : ""}${p.change_pct}%` : "";
    return `<div class="prog"><div class="top"><span class="name"><i style="background:${sportColor(p.sport)}"></i>${p.label}</span>
      <span class="verdict ${cls}">${arrow} ${p.verdict} ${chg}</span></div>
      <div class="val">${progValue(p, p.current)} <small>${p.ref_hr && p.sport !== "swim" ? `przy ${p.ref_hr} bpm` : ""}</small></div>
      <div class="cmp">${p.note ? esc(p.note) : `wcześniej ${progValue(p, p.previous)} · treningi ${p.n_current} / ${p.n_previous}`}</div>
      <div class="spark"><canvas id="spark_${p.sport}"></canvas></div></div>`;
  }).join("");
  pr.sports.forEach(p => {
    const toPlot = v => v == null ? null : p.sport === "run" ? 1000 / v : v; // run: s/km
    const lower = p.sport !== "bike"; // pace: lower = better -> reverse axis so up = better
    const opts = baseOpts();
    opts.scales.x.display = false;
    opts.scales.y.reverse = lower;
    opts.scales.y.ticks.maxTicksLimit = 3;
    opts.scales.y.ticks.callback = v => p.sport === "bike" ? Math.round(v) : mmss(v);
    opts.plugins.tooltip.callbacks = { title: i => "tydz. " + ddmm(p.weekly[i[0].dataIndex].week),
      label: c => " " + (p.sport === "bike" ? Math.round(c.raw) + " W" : mmss(c.raw) + (p.sport === "run" ? " /km" : " /100 m")) };
    draw("spark_" + p.sport, { type: "line", data: { labels: p.weekly.map(w => ddmm(w.week)),
      datasets: [{ data: p.weekly.map(w => toPlot(w.value)), borderColor: sportColor(p.sport), backgroundColor: sportColor(p.sport),
        borderWidth: 2, pointRadius: 2, pointHoverRadius: 4, tension: 0.3, spanGaps: true }] }, options: opts });
  });
  const vo = pr.vo2max, vol = pr.volume;
  const voTxt = (k, n) => vo[k].latest ? `<span>VO₂max ${n}: <b>${vo[k].latest.toFixed(1)}</b>${vo[k].eight_weeks_ago ? ` (8 tyg. temu ${vo[k].eight_weeks_ago.toFixed(1)})` : ""}</span>` : "";
  $("progressExtra").innerHTML = voTxt("run", "bieg") + voTxt("bike", "rower") +
    `<span>Objętość 4 tyg.: ${vol.map(x => `${x.label.toLowerCase()} <b>${x.current_h} h</b> (wcześniej ${x.previous_h})`).join(" · ")}</span>`;
}

/* ---------- activity analysis ---------- */
async function openActivity(id) {
  history.replaceState(null, "", "#trener");
  showTab("coach");
  C.activityId = id;
  setThread("activity:" + id);
  const panel = $("actPanel");
  panel.hidden = false;
  panel.innerHTML = `<div class="typing">Pobieram szczegóły treningu z Garmina</div>`;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    C.activity = await api(`/api/activity/${id}`);
    renderActivity();
    renderChatCtx();
  } catch (e) {
    panel.innerHTML = `<p class="err">Nie udało się otworzyć treningu: ${esc(e.message)}</p>`;
  }
}
window.openActivity = openActivity;

function renderActivity() {
  const { activity: a, detail: d, comparison: cmp, day } = C.activity;
  const sum = d.summary || {};
  const speedTxt = a.sport === "swim" ? pace100(a.avg_speed) : a.sport === "run" ? paceKm(a.avg_speed) : a.avg_speed ? (a.avg_speed * 3.6).toFixed(1) + " km/h" : "—";
  const stats = [
    ["Czas", hm(a.duration_s) + " h"], ["Dystans", distTxt(a.sport, a.distance_m)],
    [a.sport === "bike" ? "Prędkość" : "Tempo", speedTxt], ["Śr. tętno", a.avg_hr ? fmt(a.avg_hr) + " bpm" : "—"],
    ["Maks. tętno", a.max_hr ? fmt(a.max_hr) + " bpm" : "—"],
    ...(a.norm_power || a.avg_power ? [["Moc", a.norm_power ? fmt(a.norm_power) + " W NP" : fmt(a.avg_power) + " W"]] : []),
    ...(sum.averageRunCadence ? [["Kadencja", fmt(sum.averageRunCadence) + " spm"]] : []),
    ...(sum.averageSwolf ? [["SWOLF", fmt(sum.averageSwolf)]] : []),
    ["Efekt tlenowy", a.aerobic_te ? a.aerobic_te.toFixed(1) : "—"], ["Obciążenie", fmt(a.garmin_load || a.tss)],
  ];
  let cmpHtml = "";
  if (cmp && cmp.this != null && cmp.median_similar != null) {
    const v = { sport: a.sport };
    const cls = { progres: "v-progres", regres: "v-regres" }[cmp.verdict] || "v-stabilnie";
    const word = { progres: "lepiej niż zwykle", regres: "słabiej niż zwykle", stabilnie: "na Twoim zwykłym poziomie" }[cmp.verdict];
    cmpHtml = `<div class="cmp-box"><span class="verdict ${cls}">${{ progres: "▲", regres: "▼" }[cmp.verdict] || "→"} ${word} ${cmp.change_pct > 0 ? "+" : ""}${cmp.change_pct}%</span>
      &nbsp;${esc(cmp.metric)}: <b>${progValue(v, cmp.this)}</b> vs mediana <b>${progValue(v, cmp.median_similar)}</b> z ${cmp.n_similar} podobnych treningów${cmp.ref_hr && a.sport !== "swim" ? ` (przy ${cmp.ref_hr} bpm)` : ""}.</div>`;
  } else if (cmp) {
    cmpHtml = `<div class="cmp-box">Za mało podobnych treningów z tętnem, żeby porównać.</div>`;
  }
  const zones = (d.hr_zones || []).filter(z => z.zone);
  const zTotal = zones.reduce((s, z) => s + z.secs, 0);
  const zoneHtml = zTotal ? `<div class="section-t">Czas w strefach tętna</div>
    <div class="zones">${zones.map(z => `<div style="flex:${z.secs};background:var(--z${Math.min(z.zone, 5)})" title="Z${z.zone}: ${mmss(z.secs)} min"></div>`).join("")}</div>
    <div class="zone-labels">${zones.map(z => `<span><i style="background:var(--z${Math.min(z.zone, 5)})"></i>Z${z.zone}${z.low ? ` (od ${z.low})` : ""}: ${Math.round(z.secs / 60)} min · ${Math.round(z.secs / zTotal * 100)}%</span>`).join("")}</div>` : "";
  const laps = d.laps || [];
  const lapCols = [
    ["#", (l, i) => i + 1],
    ["Dystans", l => distTxt(a.sport, l.distance)],
    ["Czas", l => mmss(l.duration ?? l.movingDuration)],
    [a.sport === "bike" ? "km/h" : "Tempo", l => a.sport === "swim" ? pace100(l.averageSpeed) : a.sport === "run" ? paceKm(l.averageSpeed) : l.averageSpeed ? (l.averageSpeed * 3.6).toFixed(1) : "—"],
    ["Śr. HR", l => fmt(l.averageHR)], ["Maks. HR", l => fmt(l.maxHR)],
    ...(laps.some(l => l.averagePower) ? [["Moc", l => fmt(l.averagePower)]] : []),
    ...(laps.some(l => l.averageRunCadence) ? [["Kadencja", l => fmt(l.averageRunCadence)]] : []),
    ...(laps.some(l => l.averageSWOLF) ? [["SWOLF", l => fmt(l.averageSWOLF)]] : []),
    ...(laps.some(l => l.elevationGain) ? [["↑ m", l => fmt(l.elevationGain)]] : []),
  ];
  const lapHtml = laps.length ? `<div class="section-t">Okrążenia (${laps.length})</div><div class="tbl-wrap"><table>
    <thead><tr>${lapCols.map(([h], i) => `<th class="${i ? "r" : ""}">${h}</th>`).join("")}</tr></thead>
    <tbody>${laps.map((l, i) => `<tr>${lapCols.map(([, f], j) => `<td class="${j ? "r" : ""}">${f(l, i)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>` : "";
  const dayTxt = day ? `Tego dnia: HRV ${fmt(day.hrv_last)} ms${day.hrv_low ? ` (norma od ${fmt(day.hrv_low)})` : ""}, sen ${hm(day.sleep_s)} h, tętno spocz. ${fmt(day.rhr)}, gotowość ${fmt(day.readiness)}.` : "";
  $("actPanel").innerHTML = `<div class="act-head"><div><div class="lvl-label">Trening · ${esc(SPORT_PL[a.sport] || a.sport)}</div>
      <h2>${esc(a.name || "")}</h2><p class="sub">${ddmm(a.date)} ${a.start.slice(11, 16)} · ${esc(dayTxt)}</p></div>
      <div class="btn-row"><button class="small" id="actRefresh">Odśwież z Garmina</button><button class="small" id="actClose">Zamknij</button></div></div>
    <div class="act-stats">${stats.map(([k, v]) => `<div><span>${k}</span><b>${v}</b></div>`).join("")}</div>
    ${cmpHtml}${zoneHtml}${lapHtml}
    ${d.error || d.laps_error ? `<p class="sub err">${esc(d.error || d.laps_error)}</p>` : ""}`;
  $("actClose").onclick = closeActivity;
  $("actRefresh").onclick = async () => { C.activity = await api(`/api/activity/${a.id}?refresh=1`).catch(e => (alert(e.message), C.activity)); renderActivity(); };
}
function closeActivity() {
  C.activityId = null; C.activity = null;
  $("actPanel").hidden = true;
  setThread("general");
}

/* ---------- chat ---------- */
function setThread(t) {
  if (C.thread === t && $("msgs").dataset.loaded === t) return;
  C.thread = t;
  if (!t.startsWith("activity:")) { C.activityId = null; $("actPanel").hidden = true; }
  renderChatCtx();
  loadMessages();
}
function renderChatCtx() {
  if (C.activityId) {
    const a = C.activity && C.activity.activity;
    const name = a ? `${SPORT_PL[a.sport] || ""} ${ddmm(a.date)} · ${a.name || ""}` : "trening";
    $("chatCtx").innerHTML = `<span class="ctx-chip"><span>O treningu: ${esc(name)}</span><button title="Wróć do ogólnej rozmowy" id="ctxClose">×</button></span>`;
    $("ctxClose").onclick = closeActivity;
  } else {
    $("chatCtx").innerHTML = `<span class="ctx-chip" style="padding-right:9px"><span>Rozmowa ogólna — plan, forma, progres</span></span>`;
  }
  renderChips();
}
function renderChips() {
  const list = C.activityId
    ? ["Co poprawić w tym treningu?", "Czy trzymałem właściwe strefy?", "Jak to wypada na tle podobnych treningów?", "Jak to wpływa na plan na najbliższe dni?"]
    : ["Jak wyglądam dziś — robić zaplanowany trening?", "Ułóż plan na 2 tygodnie", "Dostosuj plan do mojej formy", "Gdzie mam progres, a gdzie regres?", "Co mówi mój skład ciała o obciążeniach?"];
  $("chips").innerHTML = list.map(t => `<button>${esc(t)}</button>`).join("");
  $("chips").querySelectorAll("button").forEach(b => b.onclick = () => send(b.textContent));
}
async function loadMessages() {
  const t = C.thread;
  try {
    const d = await api(`/api/chat?thread=${encodeURIComponent(t)}`);
    if (t !== C.thread) return;
    renderMessages(d.messages);
    $("msgs").dataset.loaded = t;
  } catch (e) { $("msgs").innerHTML = `<p class="err">${esc(e.message)}</p>`; }
}
function noteFor(meta) {
  if (!meta) return "";
  let out = "";
  const c = meta.plan_changes;
  if (c && (c.added || c.updated || c.deleted)) {
    const parts = [c.added && `dodano ${c.added}`, c.updated && `zmieniono ${c.updated}`, c.deleted && `usunięto ${c.deleted}`].filter(Boolean);
    out += `<div class="note">✓ Plan zaktualizowany: ${parts.join(", ")}</div>`;
  }
  if (c && c.errors && c.errors.length) out += `<div class="note err">Nie wszystko udało się zapisać: ${esc(c.errors.join("; "))}</div>`;
  if (meta.plan_error) out += `<div class="note err">${esc(meta.plan_error)}</div>`;
  return out;
}
function renderMessages(msgs) {
  const box = $("msgs");
  if (!msgs.length) {
    box.innerHTML = `<div class="chat-empty">${C.activityId
      ? "Zapytaj o ten trening — trener widzi okrążenia, strefy, porównanie z podobnymi sesjami i Twoją regenerację tego dnia."
      : "Trener widzi Twoje dane z Garmina, profil, plan i progres. Zacznij od jednej z sugestii poniżej."}</div>`;
    return;
  }
  box.innerHTML = msgs.map(m => m.role === "user"
    ? `<div class="msg user">${esc(m.content)}</div>`
    : `<div class="msg assistant ${(m.meta || {}).error ? "error" : ""}">${md(m.content)}${noteFor(m.meta)}</div>`).join("");
  box.scrollTop = box.scrollHeight;
}
async function send(text) {
  text = (text || "").trim();
  if (!text || C.sending) return;
  if (C.view && !C.view.llm.backend) { renderSettings(true); return; }
  C.sending = true; $("sendBtn").disabled = true;
  const box = $("msgs");
  if (box.querySelector(".chat-empty")) box.innerHTML = "";
  box.insertAdjacentHTML("beforeend", `<div class="msg user">${esc(text)}</div><div class="typing" id="typing">Trener analizuje dane</div>`);
  box.scrollTop = box.scrollHeight;
  $("chatInput").value = "";
  const thread = C.thread;
  try {
    const r = await api("/api/chat", { thread, message: text, activity_id: C.activityId });
    if (r.meta && r.meta.plan_changes) loadTrainer();
  } catch (e) {
    box.insertAdjacentHTML("beforeend", `<div class="msg assistant error">${esc(e.message)}</div>`);
  } finally {
    C.sending = false; $("sendBtn").disabled = false;
    if (thread === C.thread) loadMessages();
  }
}
$("composer").addEventListener("submit", e => { e.preventDefault(); send($("chatInput").value); });
$("chatInput").addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send($("chatInput").value); }
});
$("chatClear").onclick = async () => {
  if (!confirm("Wyczyścić tę rozmowę? Plan zostanie bez zmian.")) return;
  await api("/api/chat/clear", { thread: C.thread });
  loadMessages();
};

/* ---------- settings ---------- */
$("settingsBtn").onclick = () => renderSettings($("settings").hidden);
function renderSettings(open) {
  const st = C.view ? C.view.llm : null, box = $("settings");
  if (!st) return;
  if (!open && st.backend) { box.hidden = true; return; }
  box.hidden = false;
  const current = st.backend === "api" ? `<span class="ok-text">Połączono przez API Anthropic</span> (klucz ${esc(st.api_key_hint)}, model ${esc(st.model)})`
    : st.backend === "cli" ? `<span class="ok-text">Połączono przez Claude Code</span> — używa Twojej subskrypcji Claude.`
    : `<b>Trener nie ma jeszcze połączenia z Claude.</b>`;
  box.innerHTML = `<h3>Połączenie z Claude</h3><p style="margin:0 0 10px">${current}</p>
    <label>Klucz API Anthropic (console.anthropic.com → API keys). Zapisywany tylko na tym komputerze.</label>
    <input type="password" id="apiKey" placeholder="${st.has_api_key ? "zapisany — wpisz nowy, aby zmienić" : "sk-ant-…"}" autocomplete="off">
    <label>Model</label>
    <input id="apiModel" value="${esc(st.model)}">
    <label>Tryb</label>
    <select id="apiBackend">
      <option value="auto" ${st.preference === "auto" ? "selected" : ""}>Automatycznie (API, a gdy brak klucza — Claude Code)</option>
      <option value="api" ${st.preference === "api" ? "selected" : ""}>Tylko API</option>
      <option value="cli" ${st.preference === "cli" ? "selected" : ""}>Tylko Claude Code (subskrypcja)</option>
    </select>
    <p style="margin:0 0 10px">Bez klucza: zainstaluj Claude Code i zaloguj się (<code>claude</code> w cmd) — trener użyje Twojej subskrypcji.${st.cli_path ? ` Znaleziono: <code>${esc(st.cli_path)}</code>.` : " Nie znaleziono polecenia <code>claude</code>."}</p>
    <div class="row"><button class="primary small" id="saveSettings">Zapisz</button>
      ${st.has_api_key ? `<button class="small ghost" id="removeKey">Usuń klucz</button>` : ""}
      ${st.backend ? `<button class="small ghost" id="closeSettings">Zamknij</button>` : ""}</div>`;
  $("saveSettings").onclick = async () => {
    const body = { model: $("apiModel").value.trim(), backend: $("apiBackend").value };
    const key = $("apiKey").value.trim(); if (key) body.anthropic_api_key = key;
    C.view.llm = await api("/api/settings", body);
    renderSettings(!C.view.llm.backend);
  };
  if ($("removeKey")) $("removeKey").onclick = async () => { C.view.llm = await api("/api/settings", { anthropic_api_key: "" }); renderSettings(true); };
  if ($("closeSettings")) $("closeSettings").onclick = () => { box.hidden = true; };
}

/* ---------- init ---------- */
renderChatCtx();
loadMessages();
if (location.hash.startsWith("#trener")) showTab("coach");
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => C.view && renderProgress());
