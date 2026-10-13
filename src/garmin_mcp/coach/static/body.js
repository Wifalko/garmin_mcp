/* Skład ciała tab. Uses helpers from index.html (css, ddmm, fmt, baseOpts, draw) and coach.js ($, esc, api, showTab, setThread, send). */

const B = { data: null, files: [], editing: null };
const BODY_FIELDS = ["weight_kg", "body_fat_pct", "fat_mass_kg", "skeletal_muscle_kg", "lean_mass_kg", "water_l", "visceral_fat", "bmr_kcal", "bmi"];
const SHORT = { weight_kg: "Masa", body_fat_pct: "Tłuszcz %", fat_mass_kg: "Tłuszcz kg", skeletal_muscle_kg: "Mięśnie",
  lean_mass_kg: "Beztłuszcz.", water_l: "Woda l", visceral_fat: "Trzewny", bmr_kcal: "BMR", bmi: "BMI" };
const SEG_PL = { arm: "Ręce", leg: "Nogi" };

async function loadBody() {
  try {
    B.data = await api("/api/body");
    renderBody();
  } catch (e) {
    $("bodySignals").innerHTML = `<p class="err">Nie mogę pobrać pomiarów: ${esc(e.message)}</p>`;
  }
}
window.loadBody = loadBody;

/* ---------- upload ---------- */
$("bodyFiles").addEventListener("change", e => {
  B.files = [...e.target.files];
  $("bodyPickTxt").textContent = B.files.length ? `Wybrano: ${B.files.length} ${B.files.length === 1 ? "plik" : "plików"}` : "Wybierz zdjęcia…";
  $("bodyUpload").disabled = !B.files.length;
});

function shrink(file) {
  // Photos are scaled to ≤ 1800 px: plenty for reading a printout, much smaller to send.
  return new Promise((resolve, reject) => {
    if (file.type === "application/pdf") {
      const r = new FileReader();
      r.onload = () => resolve({ name: file.name, data_b64: r.result });
      r.onerror = () => reject(new Error(`Nie mogę odczytać ${file.name}`));
      return r.readAsDataURL(file);
    }
    const img = new Image(), url = URL.createObjectURL(file);
    img.onload = () => {
      const scale = Math.min(1, 1800 / Math.max(img.width, img.height));
      const c = document.createElement("canvas");
      c.width = Math.round(img.width * scale); c.height = Math.round(img.height * scale);
      c.getContext("2d").drawImage(img, 0, 0, c.width, c.height);
      URL.revokeObjectURL(url);
      resolve({ name: file.name.replace(/\.[^.]+$/, "") + ".jpg", data_b64: c.toDataURL("image/jpeg", 0.9) });
    };
    img.onerror = () => reject(new Error(`${file.name}: przeglądarka nie umie otworzyć tego formatu (np. HEIC z iPhone'a) — zapisz zdjęcie jako JPG.`));
    img.src = url;
  });
}

$("bodyUpload").onclick = async () => {
  if (B.data && !B.data.llm.backend) {
    $("bodyUploadStatus").innerHTML = `<p class="err">Najpierw połącz trenera z Claude: zakładka Trener → Ustawienia.</p>`;
    return;
  }
  const st = $("bodyUploadStatus");
  $("bodyUpload").disabled = true;
  try {
    st.innerHTML = `<div class="typing">Przygotowuję ${B.files.length} plik(ów)</div>`;
    const files = await Promise.all(B.files.map(shrink));
    st.innerHTML = `<div class="typing">Claude odczytuje wyniki (${files.length} plik(ów), może to potrwać minutę)</div>`;
    const r = await api("/api/body/upload", { files, fallback_date: $("bodyDate").value || null });
    const ok = r.results.filter(x => x.status !== "error");
    const bad = r.results.filter(x => x.status === "error");
    st.innerHTML = `<p class="ok-text">✓ Zapisano pomiarów: ${ok.length}${ok.length ? " (" + ok.map(x => ddmm(x.date) + (x.status === "merged" ? " uzupełniony" : "")).join(", ") + ")" : ""}.</p>` +
      ok.filter(x => x.confidence === "low" || x.notes).map(x => `<p class="sub">${ddmm(x.date)}: ${esc(x.notes || "niepewny odczyt — sprawdź wartości")}</p>`).join("") +
      bad.map(x => `<p class="err">${esc(x.error)}</p>`).join("") +
      r.unreadable.map(u => `<p class="err">${esc(u.file)}: ${esc(u.reason || "nieczytelne")}</p>`).join("");
    $("bodyFiles").value = ""; B.files = []; $("bodyPickTxt").textContent = "Wybierz zdjęcia…";
    loadBody();
  } catch (e) {
    st.innerHTML = `<p class="err">${esc(e.message)}</p>`;
    $("bodyUpload").disabled = !B.files.length;
  }
};

$("bodyAsk").onclick = () => {
  history.replaceState(null, "", "#trener");
  showTab("coach");
  setThread("general");
  send("Przeanalizuj moje pomiary składu ciała. Co mówią o tym, jak znoszę obecne obciążenia treningowe, i czy powinienem coś zmienić w planie, regeneracji albo pracy siłowej?");
};

/* ---------- render ---------- */
const val = (k, v) => v == null ? "—" : k === "bmr_kcal" ? Math.round(v) : k === "visceral_fat" ? (+v).toFixed(0) : (+v).toFixed(1);
const delta = (k, d) => {
  if (d == null) return "";
  const s = (d > 0 ? "+" : "") + (k === "bmr_kcal" ? Math.round(d) : d.toFixed(1));
  return s;
};

function renderBody() {
  const d = B.data, a = d.analysis, scans = d.scans;
  // signals
  $("bodySignals").innerHTML = !scans.length
    ? `<div class="empty">Brak pomiarów. Wgraj zdjęcia wydruków — trener uwzględni je w ocenie obciążeń i w planie.</div>`
    : a.signals.length ? a.signals.map(x => `<div class="sig"><span class="dot lvl-${x.level}" aria-hidden="true"></span><div><b>${LVL_PL[x.level]}: ${esc(x.title)}</b><br><span>${esc(x.detail)}</span></div></div>`).join("")
    : `<div class="sig"><span class="dot lvl-ok"></span><div><b>Bez niepokojących zmian</b><br><span>${a.count} pomiar(ów) od ${ddmm(a.first_date)}.</span></div></div>`;

  // tiles
  const tileKeys = ["weight_kg", "body_fat_pct", a.changes.skeletal_muscle_kg ? "skeletal_muscle_kg" : "lean_mass_kg", "fat_mass_kg", "visceral_fat", "water_l"];
  const units = { weight_kg: "kg", body_fat_pct: "%", skeletal_muscle_kg: "kg", lean_mass_kg: "kg", fat_mass_kg: "kg", visceral_fat: "", water_l: "l" };
  $("bodyTiles").innerHTML = !scans.length ? "" : tileKeys.map(k => {
    const c = a.changes[k];
    const label = (d.labels[k] || k).replace(/\s*\[.*\]/, "");
    if (!c) return `<div class="card tile"><div class="k">${label}</div><div class="v">—</div><div class="n">brak na wydrukach</div></div>`;
    const parts = [c.vs_4w != null ? `${delta(k, c.vs_4w)} w 4 tyg.` : "", c.vs_first != null ? `${delta(k, c.vs_first)} od ${ddmm(c.first_date)}` : ""].filter(Boolean);
    return `<div class="card tile"><div class="k">${label}</div><div class="v">${val(k, c.current)} <small>${units[k]}</small></div><div class="n">${parts.join(" · ") || ddmm(c.current_date)}</div></div>`;
  }).join("");

  // charts
  const labels = scans.map(s => ddmm(s.date));
  const lineChart = (id, key, color) => {
    const opts = baseOpts();
    opts.plugins.tooltip.callbacks = { title: i => scans[i[0].dataIndex].date, label: c => ` ${val(key, c.raw)}` };
    draw(id, { type: "line", data: { labels, datasets: [{ data: scans.map(s => s[key]), borderColor: color, backgroundColor: color,
      borderWidth: 2, pointRadius: 3.5, pointHoverRadius: 5, tension: 0.2, spanGaps: true }] }, options: opts });
  };
  lineChart("cBodyWeight", "weight_kg", css("--ctl"));
  lineChart("cBodyFat", "body_fat_pct", css("--bike"));
  const muscleKey = scans.some(s => s.skeletal_muscle_kg != null) ? "skeletal_muscle_kg" : "lean_mass_kg";
  $("muscleTitle").textContent = muscleKey === "skeletal_muscle_kg" ? "Mięśnie szkieletowe" : "Beztłuszczowa masa ciała";
  lineChart("cBodyMuscle", muscleKey, css("--run"));

  // segments
  const latestSeg = [...scans].reverse().find(s => s.extra && s.extra.segmental_lean_kg);
  const seg = latestSeg ? latestSeg.extra.segmental_lean_kg : null;
  if (!seg) {
    $("bodySegments").innerHTML = `<div class="empty">Brak analizy segmentowej na wydrukach.</div>`;
  } else {
    $("segSub").textContent = `Masa mięśniowa segmentów, pomiar ${ddmm(latestSeg.date)}`;
    const max = Math.max(...Object.values(seg).filter(v => v != null && v < 15), 1);
    $("bodySegments").innerHTML = ["arm", "leg"].map(p => {
      const l = seg["left_" + p], r = seg["right_" + p];
      if (l == null || r == null) return "";
      const asym = Math.abs(l - r) / ((l + r) / 2) * 100;
      const bar = (side, v) => `<div class="seg-row"><span>${side}</span><div class="seg-track"><div style="width:${v / max * 100}%"></div></div><b>${v.toFixed(2)} kg</b></div>`;
      return `<div class="seg"><div class="seg-h">${SEG_PL[p]} <span class="verdict ${asym >= 4 ? "v-regres" : "v-stabilnie"}">${asym >= 4 ? "▲" : "→"} różnica ${asym.toFixed(1)}%</span></div>${bar("Lewa", l)}${bar("Prawa", r)}</div>`;
    }).join("") + (seg.trunk != null ? `<p class="sub" style="margin-top:8px">Tułów: <b>${seg.trunk.toFixed(1)} kg</b></p>` : "");
  }

  renderBodyTable();
}

function renderBodyTable() {
  const scans = [...B.data.scans].reverse();
  if (!scans.length) { $("bodyTable").innerHTML = ""; return; }
  const head = `<thead><tr><th>Data</th>${BODY_FIELDS.map(k => `<th class="r">${SHORT[k]}</th>`).join("")}<th>Zdjęcia</th><th></th></tr></thead>`;
  const rows = scans.map(s => {
    if (B.editing === s.id) {
      return `<tr class="editing"><td><input type="date" data-f="date" value="${s.date}"></td>${BODY_FIELDS.map(k =>
        `<td class="r"><input type="number" step="any" data-f="${k}" value="${s[k] ?? ""}"></td>`).join("")}
        <td></td><td class="nowrap"><button class="small primary" data-save="${s.id}">Zapisz</button> <button class="small ghost" data-cancel>Anuluj</button></td></tr>`;
    }
    const conf = s.verified ? `<span class="badge ok" title="Sprawdzone przez Ciebie">✓</span>` : s.confidence === "low" ? `<span class="badge warn" title="${esc(s.notes || "Niepewny odczyt")}">?</span>` : "";
    return `<tr><td class="nowrap">${s.date} ${conf}${s.device ? `<br><span class="sub">${esc(s.device)}</span>` : ""}</td>
      ${BODY_FIELDS.map(k => `<td class="r">${val(k, s[k])}</td>`).join("")}
      <td class="nowrap">${s.files.map((f, i) => `<a href="/api/body/photo/${s.id}/${i}" target="_blank" rel="noopener">${i + 1}</a>`).join(" ")}</td>
      <td class="nowrap"><button class="small" data-edit="${s.id}">Popraw</button> <button class="small ghost" data-del="${s.id}">Usuń</button></td></tr>`;
  }).join("");
  $("bodyTable").innerHTML = head + `<tbody>${rows}</tbody>`;
  $("bodyTable").querySelectorAll("[data-edit]").forEach(b => b.onclick = () => { B.editing = +b.dataset.edit; renderBodyTable(); });
  $("bodyTable").querySelectorAll("[data-cancel]").forEach(b => b.onclick = () => { B.editing = null; renderBodyTable(); });
  $("bodyTable").querySelectorAll("[data-del]").forEach(b => b.onclick = async () => {
    if (!confirm("Usunąć ten pomiar? Zdjęcie zostanie w folderze body.")) return;
    await api("/api/body/delete", { id: +b.dataset.del }).catch(e => alert(e.message));
    loadBody();
  });
  $("bodyTable").querySelectorAll("[data-save]").forEach(b => b.onclick = async () => {
    const body = { id: +b.dataset.save };
    b.closest("tr").querySelectorAll("[data-f]").forEach(i => { body[i.dataset.f] = i.value === "" ? null : i.value; });
    try { await api("/api/body/update", body); B.editing = null; loadBody(); } catch (e) { alert(e.message); }
  });
}

matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => B.data && renderBody());
if (location.hash.startsWith("#sklad")) showTab("body");
