/* Metin2 AI QA Paneli — bağımlılıksız tek sayfa uygulama. */
"use strict";

const $app = document.getElementById("app");
const state = { token: null, timer: null, status: null, route: "" };

/* ------------------------------------------------------------------ yardımcılar */
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const j = (v) => esc(typeof v === "string" ? v : JSON.stringify(v));
const fmtTime = (iso) => {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return esc(iso);
  return d.toLocaleString("tr-TR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
};
const ago = (iso) => {
  if (!iso) return "—";
  const s = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${s} sn önce`;
  if (s < 3600) return `${Math.round(s / 60)} dk önce`;
  if (s < 86400) return `${Math.round(s / 3600)} sa önce`;
  return `${Math.round(s / 86400)} gün önce`;
};
const store = {
  get(k) { try { return localStorage.getItem(k); } catch { return null; } },
  set(k, v) { try { v == null ? localStorage.removeItem(k) : localStorage.setItem(k, v); } catch { /* yok say */ } },
};

function toast(msg, err = false) {
  const t = document.getElementById("toast");
  t.textContent = msg;
  t.className = "show" + (err ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = ""), 3200);
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(`/api${path}`, { method: opts.method || "GET", headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  if (res.status === 401) { askToken(); throw new Error("Yetkisiz"); }
  const ct = res.headers.get("content-type") || "";
  const data = ct.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error((data && data.error) || `HTTP ${res.status}`);
  return data;
}
const post = (path, body) => api(path, { method: "POST", body: body || {} });

function askToken() {
  const dlg = document.getElementById("loginDlg");
  if (!dlg.open) dlg.showModal();
}
document.getElementById("loginForm").addEventListener("submit", () => {
  state.token = document.getElementById("tokenInput").value;
  store.set("qa_token", state.token);
  render();
});

/* ------------------------------------------------------------------ rozetler */
const RESULT = { PASSED: ["ok", "Geçti"], FAILED: ["bad", "Başarısız"], ERROR: ["warn", "Hata"], RUNNING: ["info", "Çalışıyor"] };
const JOB = { queued: ["muted", "Kuyrukta"], running: ["info", "Çalışıyor"], done: ["ok", "Bitti"], failed: ["bad", "Başarısız"],
  cancelled: ["muted", "İptal"], interrupted: ["warn", "Yarıda kaldı"] };
const AGENT = { online: ["ok", "Online"], busy: ["info", "Meşgul"], connecting: ["warn", "Bağlanıyor"], offline: ["bad", "Koptu"],
  error: ["bad", "Hata"], stopped: ["muted", "Durduruldu"] };
const FIND = { new: ["warn", "Yeni"], confirmed: ["bad", "Doğrulandı"], flaky: ["warn", "Kararsız"], not_reproduced: ["muted", "Tekrarlanmadı"],
  regressed: ["bad", "Geriledi"], fixed: ["ok", "Düzeldi"], ignored: ["muted", "Yok sayıldı"] };
const SYS = { passed: ["ok", "Geçti"], failed: ["bad", "Başarısız"], findings: ["warn", "Bulgu var"], error: ["warn", "Hata"], untested: ["muted", "Test edilmedi"] };
const SEV = { critical: "bad", major: "bad", bug: "warn", minor: "muted", note: "muted" };
const badge = (map, key) => { const [cls, txt] = map[key] || ["muted", key || "—"]; return `<span class="badge ${cls}">${esc(txt)}</span>`; };

/* ------------------------------------------------------------------ yönlendirme */
const routes = [
  [/^$/, overview], [/^ajanlar$/, agentsPage], [/^gorev$/, newJobPage], [/^isler$/, jobsPage], [/^is\/(\d+)$/, jobPage],
  [/^run\/([\w-]+)$/, runPage], [/^bulgular$/, findingsPage], [/^bulgu\/(\d+)$/, findingPage], [/^kampanyalar$/, campaignsPage],
  [/^kampanya\/(\d+)$/, campaignPage], [/^senaryolar$/, scenariosPage], [/^senaryo\/([\w-]+)$/, scenarioPage],
];
// Canlı yenilenen sayfalar (form sayfaları yenilenmez ki yazılanlar kaybolmasın)
const LIVE = new Set(["", "ajanlar", "isler", "is", "bulgular", "kampanyalar", "kampanya"]);

async function render(soft = false) {
  const route = location.hash.replace(/^#\/?/, "");
  const changed = route !== state.route;
  state.route = route;
  document.querySelectorAll("#nav a").forEach((a) => {
    const r = a.dataset.route;
    a.classList.toggle("active", r === "" ? route === "" : route === r || route.startsWith(r.replace(/lar$|ler$/, "") + "/"));
  });
  for (const [rx, fn] of routes) {
    const m = route.match(rx);
    if (m) {
      try {
        const html = await fn(...m.slice(1));
        if (html !== undefined && (!soft || state.route === route)) {
          const y = window.scrollY;
          $app.innerHTML = html;
          if (soft) window.scrollTo(0, y);
          bind();
        }
      } catch (e) {
        if (!soft) $app.innerHTML = `<div class="card empty">Yüklenemedi: ${esc(e.message)}</div>`;
      }
      if (changed) window.scrollTo(0, 0);
      schedule(route.split("/")[0]);
      return;
    }
  }
  $app.innerHTML = `<div class="card empty">Sayfa bulunamadı. <a href="#/">Genel bakışa dön</a></div>`;
}

function schedule(base) {
  clearTimeout(state.timer);
  if (LIVE.has(base) && !document.hidden) state.timer = setTimeout(() => render(true), 3000);
}
window.addEventListener("hashchange", () => render());
document.addEventListener("visibilitychange", () => { if (!document.hidden) render(true); });

/* olay bağlama: data-act="..." düğmeleri */
function bind() {
  $app.querySelectorAll("[data-act]").forEach((el) => {
    el.addEventListener(el.tagName === "FORM" ? "submit" : "click", async (ev) => {
      ev.preventDefault();
      const act = ACTIONS[el.dataset.act];
      if (!act) return;
      el.disabled = true;
      try { await act(el, ev); } catch (e) { toast(e.message, true); } finally { el.disabled = false; }
    });
  });
  $app.querySelectorAll("tr[data-href]").forEach((tr) => tr.addEventListener("click", (ev) => {
    if (ev.target.closest("button, a")) return;
    location.hash = tr.dataset.href;
  }));
  if (state.route === "gorev") initJobForm();
}

/* ------------------------------------------------------------------ genel bakış */
function agentCard(a) {
  const s = a.snapshot || {};
  const hpPct = s.max_hp ? Math.max(0, Math.min(100, Math.round((100 * s.hp) / s.max_hp))) : 0;
  const acts = a.enabled
    ? `<button class="btn sm" data-act="agentOp" data-acc="${esc(a.account)}" data-op="reconnect">Yeniden bağla</button>
       <button class="btn sm danger" data-act="agentOp" data-acc="${esc(a.account)}" data-op="stop">Durdur</button>`
    : `<button class="btn sm primary" data-act="agentOp" data-acc="${esc(a.account)}" data-op="start">Başlat</button>`;
  return `<div class="card agent">
    <div class="top"><span class="name">${esc(a.account)}</span>${badge(AGENT, a.status)}</div>
    ${s.max_hp ? `<div class="hpbar" title="HP ${esc(s.hp)}/${esc(s.max_hp)}"><span style="width:${hpPct}%"></span></div>` : ""}
    <dl class="meta">
      <dt>Seviye</dt><dd>${esc(s.level ?? "—")}</dd>
      <dt>HP</dt><dd>${s.max_hp ? `${esc(s.hp)} / ${esc(s.max_hp)}` : "—"}</dd>
      <dt>Yang</dt><dd>${s.gold != null ? Number(s.gold).toLocaleString("tr-TR") : "—"}</dd>
      <dt>Konum</dt><dd>${s.map != null ? `harita ${esc(s.map)} · ${esc(s.x)}, ${esc(s.y)}` : "—"}</dd>
      <dt>İş</dt><dd>${a.job_id ? `<a href="#/is/${a.job_id}">#${a.job_id}</a>` : "—"}</dd>
    </dl>
    ${a.last_error && a.status !== "online" && a.status !== "busy" ? `<div class="err">${esc(a.last_error)}</div>` : ""}
    <div class="actions">${acts}</div>
  </div>`;
}

function matrixHtml(catalog, matrix) {
  const cells = catalog.map((s) => {
    const m = matrix[s.id];
    const st = (m && m.status) || s.last_status || "untested";
    const n = (m && m.scenarios ? m.scenarios.length : s.scenarios.length);
    return `<div class="cell ${esc(st)}" title="${esc(s.tags.join(", "))}"><div class="t">${esc(s.name)}</div>
      <div class="s">${(SYS[st] || ["", st])[1]} · ${n} senaryo</div></div>`;
  }).join("");
  return `<div class="matrix">${cells}</div>
    <div class="legend">${Object.keys(SYS).map((k) => badge(SYS, k)).join("")}</div>`;
}

function jobsTable(jobs, compact = false) {
  if (!jobs.length) return `<div class="empty">Henüz iş yok.</div>`;
  const rows = jobs.map((jb) => {
    const p = jb.progress || {};
    const pct = p.total ? Math.round((100 * (p.done || 0)) / p.total) : null;
    return `<tr data-href="#/is/${jb.job_id}" class="clickable">
      <td class="mono">#${jb.job_id}</td><td>${esc(jobTitle(jb))}</td><td>${badge(JOB, jb.status)}</td>
      <td>${pct != null && jb.status === "running" ? `<div class="progress" title="${p.done}/${p.total}"><span style="width:${pct}%"></span></div><small>${esc(p.current || "")}</small>` : (jb.run_ids ? `${jb.run_ids.length} run` : "—")}</td>
      ${compact ? "" : `<td><small>${esc(jb.source || "")}</small></td>`}
      <td><small>${ago(jb.finished_at || jb.started_at || jb.created_at)}</small></td>
      ${compact ? "" : `<td>${["queued", "running"].includes(jb.status) ? `<button class="btn sm danger" data-act="cancelJob" data-id="${jb.job_id}">İptal</button>` : ""}</td>`}
    </tr>`;
  }).join("");
  return `<div class="table-wrap"><table><thead><tr><th>İş</th><th>Tür</th><th>Durum</th><th>İlerleme</th>${compact ? "" : "<th>Kaynak</th>"}<th>Zaman</th>${compact ? "" : "<th></th>"}</tr></thead><tbody>${rows}</tbody></table></div>`;
}

function jobTitle(jb) {
  const p = jb.params || {};
  switch (jb.type) {
    case "scenario": return `Senaryo · ${p.name}`;
    case "suite": return p.tag ? `Suite · #${p.tag}` : "Suite · tümü";
    case "affected": return "Değişikliğe göre";
    case "explore": return `Keşif · ${(p.goal || "").slice(0, 50)}${(p.goal || "").length > 50 ? "…" : ""}`;
    case "campaign": return p.systems && p.systems.length ? `Kampanya · ${p.systems.join(", ")}` : "Kampanya · her şey";
    case "replay": return `Replay · ${p.run_id}`;
    case "confirm": return `Bulgu doğrulama · #${p.finding_id}`;
    default: return jb.type;
  }
}

function findingsTable(items) {
  if (!items.length) return `<div class="empty">Bulgu yok. 🎉</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>#</th><th>Bulgu</th><th>Durum</th><th>Önem</th><th>Sistem</th><th class="num">Tekrar</th><th>Son görülme</th></tr></thead><tbody>
    ${items.map((f) => `<tr data-href="#/bulgu/${f.id}" class="clickable"><td class="mono">${f.id}</td><td>${esc(f.title)}</td>
      <td>${badge(FIND, f.status)}</td><td><span class="badge ${SEV[f.severity] || "muted"}">${esc(f.severity || "—")}</span></td>
      <td>${esc(f.system || "—")}</td><td class="num">${f.occurrences}</td><td><small>${ago(f.last_seen)}</small></td></tr>`).join("")}
  </tbody></table></div>`;
}

async function overview() {
  const [st, agents, jobs, findings, catalog] = await Promise.all([
    api("/status"), api("/agents"), api("/jobs?limit=8"), api("/findings?status=new,confirmed,flaky,regressed&limit=8"), api("/catalog"),
  ]);
  state.status = st;
  updateEnv(st);
  const lc = st.last_campaign;
  const active = Object.entries(st.findings || {}).filter(([k]) => ["new", "confirmed", "flaky", "regressed"].includes(k)).reduce((a, [, v]) => a + v, 0);
  const online = (st.agents.online || 0) + (st.agents.busy || 0);
  const total = Object.values(st.agents).reduce((a, b) => a + b, 0);
  const counts = (lc && lc.summary && lc.summary.counts) || {};
  return `
  <div class="page-head"><div><h1>Genel bakış</h1><div class="muted">AI oyuncular test sunucusunda 7/24 çalışır. Mod: <b>${esc(st.bridge_mode)}</b></div></div>
    <div class="row"><button class="btn primary" data-act="quickCampaign">Her şeyi test et</button><a class="btn" href="#/gorev">Görev ver</a></div></div>
  <div class="grid cols-4">
    <div class="card stat"><div class="label">Ajanlar</div><div class="value">${online}<small> / ${total}</small></div><div class="sub">${st.agents.busy || 0} meşgul · ${(st.agents.offline || 0) + (st.agents.error || 0)} kopuk</div></div>
    <div class="card stat"><div class="label">İşler</div><div class="value">${st.jobs.running}</div><div class="sub">çalışıyor · ${st.jobs.queued} kuyrukta</div></div>
    <div class="card stat"><div class="label">Açık bulgu</div><div class="value">${active}</div><div class="sub">${st.findings.confirmed || 0} doğrulandı · ${st.findings.fixed || 0} düzeldi</div></div>
    <div class="card stat"><div class="label">Son kampanya</div><div class="value">${lc ? `${counts.passed || 0}<small> / ${lc.summary && lc.summary.systems || "?"}</small>` : "—"}</div>
      <div class="sub">${lc ? `${lc.status === "done" ? "sistem geçti" : esc(lc.status)} · ${ago(lc.finished_at || lc.started_at)}` : "henüz yok"}</div></div>
  </div>
  <div class="stack" style="margin-top:14px">
    <div class="card"><div class="row" style="justify-content:space-between"><h2>Sistem sağlığı</h2>${lc ? `<a href="#/kampanya/${lc.id}">Kampanya #${lc.id} →</a>` : ""}</div>
      ${matrixHtml(catalog, (lc && lc.matrix) || {})}</div>
    <div><h3>Ajanlar</h3><div class="agents">${agents.map(agentCard).join("") || `<div class="card empty">Ajan yok.</div>`}</div></div>
    <div class="grid cols-2">
      <div class="card"><div class="row" style="justify-content:space-between"><h2>Son işler</h2><a href="#/isler">Tümü →</a></div>${jobsTable(jobs, true)}</div>
      <div class="card"><div class="row" style="justify-content:space-between"><h2>Açık bulgular</h2><a href="#/bulgular">Tümü →</a></div>${findingsTable(findings)}</div>
    </div>
  </div>`;
}

function updateEnv(st) {
  const b = document.getElementById("envBadge");
  b.textContent = `${st.env} · ${st.bridge_mode}${st.llm.available ? " · LLM" : ""}`;
}

/* ------------------------------------------------------------------ ajanlar */
async function agentsPage() {
  const agents = await api("/agents");
  const rows = agents.map((a) => `<tr><td class="mono">${esc(a.account)}</td><td>${badge(AGENT, a.status)}</td>
    <td>${a.keep_online ? "Evet" : "Hayır"}</td><td>${a.job_id ? `<a href="#/is/${a.job_id}">#${a.job_id}</a>` : "—"}</td>
    <td class="num">${a.reconnects}</td><td><small>${esc(a.last_error || "")}</small></td>
    <td><div class="row">${a.enabled
      ? `<button class="btn sm" data-act="agentOp" data-acc="${esc(a.account)}" data-op="reconnect">Yeniden bağla</button><button class="btn sm danger" data-act="agentOp" data-acc="${esc(a.account)}" data-op="stop">Durdur</button>`
      : `<button class="btn sm primary" data-act="agentOp" data-acc="${esc(a.account)}" data-op="start">Başlat</button><button class="btn sm danger" data-act="removeAgent" data-acc="${esc(a.account)}">Sil</button>`}</div></td></tr>`).join("");
  return `<div class="page-head"><div><h1>Ajanlar</h1><div class="muted">Her ajan bir <code>AI_QA_*</code> hesabıdır; "online tut" açıkken işler arasında oyunda kalır ve koparsa kendini yeniden bağlar.</div></div></div>
  <div class="stack">
    <div class="agents">${agents.map(agentCard).join("")}</div>
    <div class="card"><h2>Liste</h2><div class="table-wrap"><table><thead><tr><th>Hesap</th><th>Durum</th><th>Online tut</th><th>İş</th><th class="num">Yeniden bağlanma</th><th>Son hata</th><th></th></tr></thead><tbody>${rows}</tbody></table></div></div>
    <form class="card form" data-act="addAgent"><h2>Ajan ekle</h2>
      <div class="grid cols-2"><label>Hesap <input type="text" name="account" placeholder="AI_QA_005" required pattern="AI_QA_.+"><span class="hint">Yalnızca AI_QA_ ile başlayan hesaplar</span></label>
      <label>Karakter <input type="text" name="character" placeholder="(boşsa hesapla aynı)"></label></div>
      <div class="row"><label class="checks"><label><input type="checkbox" name="keep_online" checked> Online tut</label></label></div>
      <div class="row end"><button class="btn primary" type="submit">Ekle ve başlat</button></div></form>
  </div>`;
}

/* ------------------------------------------------------------------ görev ver */
async function newJobPage() {
  const [scenarios, catalog, st] = await Promise.all([api("/scenarios"), api("/catalog"), api("/status")]);
  const tags = [...new Set(scenarios.flatMap((s) => s.tags || []))].sort();
  const llm = st.llm.available;
  state.formData = { scenarios };
  return `<div class="page-head"><div><h1>Görev ver</h1><div class="muted">İş kuyruğa girer; boştaki ajanlar alır ve raporu panele yazar.</div></div></div>
  <form class="card form" data-act="submitJob" id="jobForm">
    <label>Görev türü<div class="seg" id="typeSeg">
      <button type="button" data-type="campaign" class="active">Her şeyi test et</button>
      <button type="button" data-type="scenario">Senaryo</button>
      <button type="button" data-type="suite">Suite (etiket)</button>
      <button type="button" data-type="affected">Değişen dosyalar</button>
      <button type="button" data-type="explore" ${llm ? "" : "disabled title='LLM anahtarı tanımlı değil (GEMINI_API_KEY)'"}>Serbest keşif (AI)</button>
    </div></label>
    <input type="hidden" name="type" value="campaign">
    <div data-for="campaign" class="stack">
      <label>Sistemler <span class="hint">Boş bırakılırsa katalogdaki tüm sistemler</span>
        <div class="checks">${catalog.map((s) => `<label><input type="checkbox" name="systems" value="${esc(s.id)}"> ${esc(s.name)} <small>(${s.scenarios.length})</small></label>`).join("")}</div></label>
      <label class="checks"><label><input type="checkbox" name="explore" ${llm ? "checked" : "disabled"}> Her sistemde AI keşfi de yap ${llm ? "" : "<small>(LLM anahtarı yok)</small>"}</label></label>
    </div>
    <div data-for="scenario" hidden><label>Senaryo<select name="name">${scenarios.map((s) => `<option value="${esc(s.name)}">${esc(s.name)} — ${esc((s.description || "").slice(0, 70))}</option>`).join("")}</select></label></div>
    <div data-for="suite" hidden><label>Etiket<select name="tag"><option value="">(tüm senaryolar)</option>${tags.map((t) => `<option>${esc(t)}</option>`).join("")}</select></label></div>
    <div data-for="affected" hidden><label>Değişen dosyalar <span class="hint">Her satıra bir yol. Boşsa kaynak repodaki git diff kullanılır.</span><textarea name="files" placeholder="game/src/shop.cpp&#10;game/src/exchange.cpp"></textarea></label>
      <label>git diff tabanı<input type="text" name="base" value="HEAD"></label></div>
    <div data-for="explore" hidden>
      <label>Hedef <span class="hint">AI oyuncunun ne test edeceği; serbest metin</span><textarea name="goal" placeholder="Genel Mağaza'yı oyuncu gibi kullan; yetersiz yang, dolu envanter ve art arda alım-satım durumlarını dene."></textarea></label>
      <div class="grid cols-2"><label>Adım bütçesi<input type="number" name="max_steps" min="5" max="500" value="40"></label>
      <label>Senaryo olarak kaydet <span class="hint">opsiyonel ad (auto_…)</span><input type="text" name="save_as" pattern="[A-Za-z0-9_-]*"></label></div>
    </div>
    <label>Seed <span class="hint">opsiyonel — aynı seed ile tekrar üretilebilir</span><input type="number" name="seed" min="0"></label>
    <div class="row end"><button class="btn primary" type="submit">Kuyruğa ekle</button></div>
  </form>`;
}

function initJobForm() {
  const form = document.getElementById("jobForm");
  const seg = document.getElementById("typeSeg");
  seg.querySelectorAll("button").forEach((b) => b.addEventListener("click", () => {
    if (b.disabled) return;
    seg.querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
    form.type.value = b.dataset.type;
    form.querySelectorAll("[data-for]").forEach((s) => (s.hidden = s.dataset.for !== b.dataset.type));
  }));
}

/* ------------------------------------------------------------------ işler */
async function jobsPage() {
  const jobs = await api("/jobs?limit=100");
  return `<div class="page-head"><div><h1>İşler</h1><div class="muted">Kuyruk ve geçmiş. Zamanlanmış işler <code>schedule:</code> kaynağıyla görünür.</div></div><a class="btn primary" href="#/gorev">Görev ver</a></div>
  <div class="card">${jobsTable(jobs)}</div>`;
}

async function jobPage(id) {
  const jb = await api(`/jobs/${id}`);
  const r = jb.result || {};
  const runs = jb.run_ids || [];
  let resultHtml = "";
  if (jb.type === "campaign" && r.campaign_id) resultHtml = `<p><a class="btn" href="#/kampanya/${r.campaign_id}">Kampanya raporunu aç →</a></p>`;
  if (r.results) resultHtml += `<div class="table-wrap"><table><thead><tr><th>Senaryo</th><th>Sonuç</th><th>Run</th></tr></thead><tbody>${r.results.map((x) => `<tr data-href="#/run/${x.run_id}" class="clickable"><td>${esc(x.scenario)}</td><td>${badge(RESULT, x.result)}</td><td class="mono">${esc(x.run_id)}</td></tr>`).join("")}</tbody></table></div>`;
  if (jb.type === "explore" && r.run_id) resultHtml += exploreSummary(r);
  if (r.verdict) resultHtml += `<p><b>${esc(r.verdict)}</b></p>`;
  return `<div class="page-head"><div><h1>İş #${jb.job_id} ${badge(JOB, jb.status)}</h1><div class="muted">${esc(jobTitle(jb))}</div></div>
    ${["queued", "running"].includes(jb.status) ? `<button class="btn danger" data-act="cancelJob" data-id="${jb.job_id}">İptal et</button>` : ""}</div>
  <div class="grid cols-2">
    <div class="card"><h2>Bilgi</h2><dl class="kv"><dt>Tür</dt><dd>${esc(jb.type)}</dd><dt>Kaynak</dt><dd>${esc(jb.source)}</dd>
      <dt>Oluşturuldu</dt><dd>${fmtTime(jb.created_at)}</dd><dt>Başladı</dt><dd>${fmtTime(jb.started_at)}</dd><dt>Bitti</dt><dd>${fmtTime(jb.finished_at)}</dd>
      <dt>Ajanlar</dt><dd>${esc((jb.agents || []).join(", ") || "—")}</dd><dt>Parametreler</dt><dd class="mono">${j(jb.params)}</dd>
      ${jb.progress ? `<dt>İlerleme</dt><dd>${esc(jb.progress.done ?? "")}${jb.progress.total ? " / " + esc(jb.progress.total) : ""} ${esc(jb.progress.current || "")}</dd>` : ""}</dl>
      ${jb.error ? `<div class="fail-box" style="margin-top:12px"><b>Hata:</b> ${esc(jb.error)}</div>` : ""}</div>
    <div class="card"><h2>Run'lar (${runs.length})</h2>${runs.length ? `<div class="row">${runs.map((x) => `<a class="badge info" href="#/run/${x}">${esc(x)}</a>`).join("")}</div>` : `<div class="empty">Henüz run yok.</div>`}</div>
  </div>
  ${resultHtml ? `<div class="card" style="margin-top:14px"><h2>Sonuç</h2>${resultHtml}</div>` : ""}`;
}

function exploreSummary(r) {
  return `<dl class="kv"><dt>Run</dt><dd><a href="#/run/${esc(r.run_id)}">${esc(r.run_id)}</a></dd><dt>Durma</dt><dd>${esc(r.stop_reason)}</dd>
    <dt>Adım / tur</dt><dd>${esc(r.steps)} / ${esc(r.turns)}</dd><dt>Token</dt><dd>${esc((r.usage || {}).total_tokens ?? "—")}</dd>
    <dt>AI özeti</dt><dd>${esc(r.agent_summary || "—")}</dd>${r.saved_scenario ? `<dt>Senaryo</dt><dd class="mono">${esc(r.saved_scenario)}</dd>` : ""}</dl>
    ${(r.findings || []).length ? `<h3 style="margin-top:12px">AI bulguları</h3><ul>${r.findings.map((f) => `<li><span class="badge ${SEV[f.severity] || "muted"}">${esc(f.severity)}</span> <b>${esc(f.title)}</b> — ${esc(f.description)}</li>`).join("")}</ul>` : ""}`;
}

/* ------------------------------------------------------------------ run raporu */
async function runPage(id) {
  const [rep, trace, logs] = await Promise.all([api(`/runs/${id}`), api(`/runs/${id}/trace?tail=400`), api(`/runs/${id}/logs?tail=300`)]);
  const f = rep.failure;
  const shots = (rep.evidence && rep.evidence.screenshots) || [];
  const steps = (rep.steps || []).map((s) => `<tr><td class="num">${esc(s.index)}</td><td>${s.agent ? `<b>${esc(s.agent)}:</b> ` : ""}${esc(s.name)}</td>
    <td class="mono"><small>${j(s.args || {})}</small></td><td>${badge({ passed: ["ok", "Geçti"], failed: ["bad", "Başarısız"], skipped: ["muted", "Atlandı"] }, s.status)}</td>
    <td><small>${esc(s.error || "")}</small></td></tr>`).join("");
  const asserts = (rep.assertions || []).map((a) => `<tr><td class="num">${esc(a.step ?? "son")}</td><td>${a.agent ? `<b>${esc(a.agent)}:</b> ` : ""}${esc(a.name)}</td>
    <td>${a.passed ? badge(RESULT, "PASSED") : badge(RESULT, "FAILED")}</td><td class="mono"><small>${j(a.expected)}</small></td><td class="mono"><small>${j(a.actual)}</small></td></tr>`).join("");
  return `<div class="page-head"><div><div class="result-head"><h1>${esc(rep.run_id)}</h1>${badge(RESULT, rep.result)}<span class="muted">${esc(rep.scenario)} · ${esc(rep.mode)}</span></div>
    <div class="muted">seed ${esc(rep.seed)} · ${fmtTime(rep.started_at)} · oyun süresi ${Math.round((rep.game_time_ms || 0) / 1000)} sn${rep.agents ? " · ajanlar " + esc(Object.values(rep.agents).join(", ")) : " · " + esc(rep.player || "")}</div></div>
    ${rep.mode !== "explore" && rep.result !== "RUNNING" ? `<button class="btn" data-act="replayRun" data-id="${esc(rep.run_id)}">Tekrar oynat (3×)</button>` : ""}</div>
  <div class="stack">
    ${f ? `<div class="fail-box"><h2>İlk hata — ${f.step != null ? `adım ${esc(f.step)}` : "final"}${f.agent ? ` (${esc(f.agent)})` : ""}: ${esc(f.name)}</h2>
      <dl class="kv"><dt>Mesaj</dt><dd>${esc(f.message || "")}</dd><dt>Beklenen</dt><dd class="mono">${j(f.expected)}</dd><dt>Gerçekleşen</dt><dd class="mono">${j(f.actual)}</dd>
      ${(rep.failures || []).length > 1 ? `<dt>Toplam</dt><dd>${rep.failures.length} hata</dd>` : ""}</dl></div>` : ""}
    ${rep.error ? `<div class="fail-box"><b>Altyapı hatası:</b> ${esc(rep.error)}</div>` : ""}
    ${rep.mode === "explore" ? `<div class="card"><h2>Keşif</h2><dl class="kv"><dt>Hedef</dt><dd>${esc(rep.goal || "")}</dd><dt>AI özeti</dt><dd>${esc(rep.agent_summary || "—")}</dd>
      <dt>Model</dt><dd>${esc(((rep.llm || {}).provider || "") + " " + ((rep.llm || {}).model || ""))}</dd><dt>Token</dt><dd>${esc((((rep.llm || {}).usage) || {}).total_tokens ?? "—")}</dd></dl>
      ${(rep.findings || []).length ? `<ul>${rep.findings.map((x) => `<li><span class="badge ${SEV[x.severity] || "muted"}">${esc(x.severity)}</span> <b>${esc(x.title)}</b> — ${esc(x.description)}</li>`).join("")}</ul>` : ""}</div>` : ""}
    <div class="card"><h2>Adımlar</h2>${steps ? `<div class="table-wrap"><table><thead><tr><th class="num">#</th><th>Adım</th><th>Parametre</th><th>Durum</th><th>Hata</th></tr></thead><tbody>${steps}</tbody></table></div>` : `<div class="empty">Adım yok.</div>`}</div>
    <div class="card"><h2>Kontroller (oracle)</h2>${asserts ? `<div class="table-wrap"><table><thead><tr><th class="num">Adım</th><th>Kontrol</th><th>Sonuç</th><th>Beklenen</th><th>Gerçekleşen</th></tr></thead><tbody>${asserts}</tbody></table></div>` : `<div class="empty">Kontrol yok.</div>`}</div>
    ${shots.length ? `<div class="card"><h2>Ekran görüntüleri</h2><div class="shots">${shots.map((s) => `<figure><a href="/api/runs/${esc(rep.run_id)}/files/${esc(s)}" target="_blank" data-img="${esc(s)}"><img alt="${esc(s)}" data-src="/api/runs/${esc(rep.run_id)}/files/${esc(s)}"></a><figcaption>${esc(s.replace("screenshots/", ""))}</figcaption></figure>`).join("")}</div></div>` : ""}
    <details class="card" open><summary>Action trace</summary><pre class="block" style="margin-top:10px">${esc(trace.text || "")}</pre></details>
    <details class="card"><summary>Sunucu logları</summary><pre class="block" style="margin-top:10px">${esc(logs.server || "")}</pre></details>
    <details class="card"><summary>Client logları</summary><pre class="block" style="margin-top:10px">${esc(logs.client || "")}</pre></details>
  </div>`;
}

/* Ekran görüntüleri yetkili fetch ile yüklenir (şifre başlıkta gider) */
async function loadImages() {
  for (const img of $app.querySelectorAll("img[data-src]")) {
    const src = img.dataset.src;
    img.removeAttribute("data-src");
    try {
      const res = await fetch(src, { headers: state.token ? { Authorization: `Bearer ${state.token}` } : {} });
      if (res.ok) {
        const url = URL.createObjectURL(await res.blob());
        img.src = url;
        const a = img.closest("a");
        if (a) a.href = url; // büyük görüntü de yetkili blob'dan açılsın
      }
    } catch { /* yok say */ }
  }
}
new MutationObserver(loadImages).observe($app, { childList: true });

/* ------------------------------------------------------------------ bulgular */
async function findingsPage() {
  const sel = state.findFilter ?? "new,confirmed,flaky,regressed,not_reproduced";
  const items = await api(`/findings${sel ? `?status=${sel}` : ""}`);
  const opt = (v, t) => `<button type="button" class="${sel === v ? "active" : ""}" data-act="findFilter" data-v="${v}">${t}</button>`;
  return `<div class="page-head"><div><h1>Bulgular</h1><div class="muted">Aynı hata tekrar görüldüğünde yeni kayıt açılmaz; tekrar sayısı artar. Yeni bulgular otomatik olarak tekrar oynatılarak doğrulanır.</div></div></div>
  <div class="seg" style="margin-bottom:12px">${opt("new,confirmed,flaky,regressed,not_reproduced", "Açık")}${opt("confirmed,regressed", "Doğrulanmış")}${opt("fixed", "Düzelen")}${opt("ignored", "Yok sayılan")}${opt("", "Tümü")}</div>
  <div class="card">${findingsTable(items)}</div>`;
}

async function findingPage(id) {
  const f = await api(`/findings/${id}`);
  const d = f.details || {};
  const statuses = ["new", "confirmed", "flaky", "not_reproduced", "regressed", "fixed", "ignored"];
  return `<div class="page-head"><div><h1>Bulgu #${f.id}</h1><div class="result-head">${badge(FIND, f.status)}<span class="badge ${SEV[f.severity] || "muted"}">${esc(f.severity)}</span><b>${esc(f.title)}</b></div></div>
    <button class="btn" data-act="confirmFinding" data-id="${f.id}">Tekrar doğrula</button></div>
  <div class="grid cols-2">
    <div class="card"><h2>Ayrıntı</h2><dl class="kv"><dt>Mesaj</dt><dd>${esc(d.message || "")}</dd><dt>Beklenen</dt><dd class="mono">${j(d.expected)}</dd>
      <dt>Gerçekleşen</dt><dd class="mono">${j(d.actual)}</dd><dt>Adım</dt><dd>${esc(d.step ?? "—")}${d.agent ? ` (${esc(d.agent)})` : ""}</dd>
      <dt>Senaryo</dt><dd>${f.scenario ? `<a href="#/senaryo/${esc(f.scenario)}">${esc(f.scenario)}</a>` : "—"}</dd><dt>Sistem</dt><dd>${esc(f.system || "—")}</dd>
      <dt>İlk / son</dt><dd>${fmtTime(f.first_seen)} → ${fmtTime(f.last_seen)}</dd><dt>Tekrar</dt><dd>${f.occurrences} kez</dd>
      <dt>Doğrulama</dt><dd>${f.confirm ? `${esc(f.confirm.verdict)} <small>(${fmtTime(f.confirm.at)})</small>` : "—"}</dd>
      ${f.fixed_at ? `<dt>Düzeldi</dt><dd>${fmtTime(f.fixed_at)} · <a href="#/run/${esc(f.fixed_by_run)}">${esc(f.fixed_by_run)}</a></dd>` : ""}</dl></div>
    <form class="card form" data-act="updateFinding" data-id="${f.id}"><h2>Durum ve not</h2>
      <label>Durum<select name="status">${statuses.map((s) => `<option value="${s}" ${s === f.status ? "selected" : ""}>${(FIND[s] || [0, s])[1]}</option>`).join("")}</select></label>
      <label>Not<textarea name="note" placeholder="ör. geliştirici notu, issue bağlantısı">${esc(f.note || "")}</textarea></label>
      <div class="row end"><button class="btn primary" type="submit">Kaydet</button></div></form>
  </div>
  <div class="card" style="margin-top:14px"><h2>Görüldüğü run'lar</h2><div class="row">${(f.run_ids || []).slice().reverse().map((r) => `<a class="badge info" href="#/run/${esc(r)}">${esc(r)}</a>`).join("")}</div></div>`;
}

/* ------------------------------------------------------------------ kampanyalar */
async function campaignsPage() {
  const items = await api("/campaigns");
  const rows = items.map((c) => {
    const s = c.summary || {};
    const k = s.counts || {};
    return `<tr data-href="#/kampanya/${c.id}" class="clickable"><td class="mono">#${c.id}</td><td>${badge({ done: ["ok", "Bitti"], running: ["info", "Çalışıyor"], failed: ["bad", "Başarısız"], cancelled: ["muted", "İptal"] }, c.status)}</td>
      <td>${Object.entries(k).map(([st, n]) => `${badge(SYS, st)} ${n}`).join(" ")}</td>
      <td>${(s.regressions || []).length ? `<span class="badge bad">${s.regressions.length} yeni kırmızı</span>` : ""}${(s.fixed || []).length ? ` <span class="badge ok">${s.fixed.length} düzeldi</span>` : ""}</td>
      <td><small>${fmtTime(c.started_at)}</small></td><td class="num">${s.duration_s ?? "—"} sn</td></tr>`;
  }).join("");
  return `<div class="page-head"><div><h1>Kampanyalar</h1><div class="muted">"Her şeyi test et" çalıştırmaları: katalogdaki her sistem senaryoları (ve varsa AI keşfi) ile test edilir.</div></div>
    <button class="btn primary" data-act="quickCampaign">Yeni kampanya</button></div>
  <div class="card">${rows ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>Durum</th><th>Sistemler</th><th>Değişim</th><th>Başlangıç</th><th class="num">Süre</th></tr></thead><tbody>${rows}</tbody></table></div>` : `<div class="empty">Henüz kampanya yok.</div>`}</div>`;
}

async function campaignPage(id) {
  const [c, catalog] = await Promise.all([api(`/campaigns/${id}`), api("/catalog")]);
  const m = c.matrix || {};
  const s = c.summary || {};
  const names = Object.fromEntries(catalog.map((x) => [x.id, x.name]));
  const rows = Object.entries(m).map(([sid, v]) => `<tr><td><b>${esc(v.name || names[sid] || sid)}</b>${(s.regressions || []).includes(sid) ? ' <span class="badge bad">yeni kırmızı</span>' : ""}</td>
    <td>${badge(SYS, v.status)}</td>
    <td>${(v.scenarios || []).map((r) => `<a href="#/run/${esc(r.run_id)}">${badge(RESULT, r.result)} ${esc(r.scenario)}</a>`).join("<br>") || '<span class="muted">senaryo yok</span>'}</td>
    <td>${v.explore ? (v.explore.error ? `<small class="muted">${esc(v.explore.error)}</small>` : `<a href="#/run/${esc(v.explore.run_id)}">${esc(v.explore.findings)} bulgu</a>`) : "—"}</td>
    <td class="num">${esc(v.duration_s)} sn</td></tr>`).join("");
  return `<div class="page-head"><div><h1>Kampanya #${c.id}</h1><div class="muted">${fmtTime(c.started_at)} → ${fmtTime(c.finished_at)} · ${esc(s.duration_s ?? "—")} sn${s.previous_campaign ? ` · önceki: <a href="#/kampanya/${s.previous_campaign}">#${s.previous_campaign}</a>` : ""}</div></div></div>
  <div class="stack">
    <div class="card"><h2>Sistem matrisi</h2>${matrixHtml(catalog, m)}</div>
    <div class="card"><h2>Ayrıntı</h2><div class="table-wrap"><table><thead><tr><th>Sistem</th><th>Durum</th><th>Senaryolar</th><th>AI keşfi</th><th class="num">Süre</th></tr></thead><tbody>${rows}</tbody></table></div></div>
  </div>`;
}

/* ------------------------------------------------------------------ senaryolar */
async function scenariosPage() {
  const [items, catalog] = await Promise.all([api("/scenarios"), api("/catalog")]);
  return `<div class="page-head"><div><h1>Senaryolar ve sistem kataloğu</h1><div class="muted">Yeni senaryoları Claude <code>write_scenario</code> ile ekler; AI keşfi de senaryo üretebilir.</div></div></div>
  <div class="grid cols-2">
    <div class="card"><h2>Senaryolar (${items.length})</h2><div class="table-wrap"><table><thead><tr><th>Ad</th><th>Etiketler</th><th class="num">Adım</th></tr></thead><tbody>
      ${items.map((s) => `<tr data-href="#/senaryo/${esc(s.name)}" class="clickable"><td><b>${esc(s.name)}</b><br><small>${esc(s.description || s.error || "")}</small></td><td>${(s.tags || []).map((t) => `<span class="badge muted">${esc(t)}</span>`).join(" ")}</td><td class="num">${esc(s.steps ?? "")}</td></tr>`).join("")}
    </tbody></table></div></div>
    <div class="card"><h2>Sistem kataloğu (${catalog.length})</h2><div class="table-wrap"><table><thead><tr><th>Sistem</th><th>Senaryolar</th><th>Son durum</th></tr></thead><tbody>
      ${catalog.map((s) => `<tr><td><b>${esc(s.name)}</b><br><small class="mono">${esc(s.id)}</small></td><td>${s.scenarios.map((n) => `<a href="#/senaryo/${esc(n)}">${esc(n)}</a>`).join("<br>") || '<span class="muted">yok</span>'}</td><td>${badge(SYS, s.last_status || "untested")}</td></tr>`).join("")}
    </tbody></table></div></div>
  </div>`;
}

async function scenarioPage(name) {
  const s = await api(`/scenarios/${name}`);
  return `<div class="page-head"><div><h1>${esc(s.name)}</h1></div><button class="btn primary" data-act="runScenario" data-name="${esc(s.name)}">Şimdi çalıştır</button></div>
  <div class="card"><pre class="block">${esc(s.yaml)}</pre></div>`;
}

/* ------------------------------------------------------------------ aksiyonlar */
const ACTIONS = {
  async agentOp(el) { await post(`/agents/${encodeURIComponent(el.dataset.acc)}/${el.dataset.op}`); toast("Tamam"); render(true); },
  async removeAgent(el) { if (!confirm(`${el.dataset.acc} silinsin mi?`)) return; await api(`/agents/${encodeURIComponent(el.dataset.acc)}`, { method: "DELETE" }); render(true); },
  async addAgent(form) {
    const fd = new FormData(form);
    await post("/agents", { account: fd.get("account"), character: fd.get("character") || null, keep_online: fd.get("keep_online") === "on" });
    toast("Ajan eklendi"); render(true);
  },
  async cancelJob(el) { await post(`/jobs/${el.dataset.id}/cancel`); toast("İptal istendi"); render(true); },
  async quickCampaign() {
    const jb = await post("/jobs", { type: "campaign", params: { explore: !!(state.status && state.status.llm.available) } });
    toast(`Kampanya kuyrukta (#${jb.job_id})`); location.hash = `#/is/${jb.job_id}`;
  },
  async submitJob(form) {
    const fd = new FormData(form);
    const type = fd.get("type");
    const params = {};
    const seed = fd.get("seed");
    if (seed) params.seed = Number(seed);
    if (type === "campaign") { const sys = fd.getAll("systems"); if (sys.length) params.systems = sys; params.explore = fd.get("explore") === "on"; }
    if (type === "scenario") params.name = fd.get("name");
    if (type === "suite" && fd.get("tag")) params.tag = fd.get("tag");
    if (type === "affected") { const files = String(fd.get("files") || "").split("\n").map((x) => x.trim()).filter(Boolean); if (files.length) params.files = files; else params.base = fd.get("base") || "HEAD"; }
    if (type === "explore") { params.goal = fd.get("goal"); params.max_steps = Number(fd.get("max_steps") || 40); if (fd.get("save_as")) params.save_as = fd.get("save_as"); }
    const jb = await post("/jobs", { type, params });
    toast(`İş kuyrukta (#${jb.job_id})`); location.hash = `#/is/${jb.job_id}`;
  },
  async replayRun(el) { const jb = await post(`/runs/${el.dataset.id}/replay`, { times: 3 }); toast(`Replay kuyrukta (#${jb.job_id})`); location.hash = `#/is/${jb.job_id}`; },
  async confirmFinding(el) { const jb = await post(`/findings/${el.dataset.id}/confirm`); toast(`Doğrulama kuyrukta (#${jb.job_id})`); location.hash = `#/is/${jb.job_id}`; },
  async updateFinding(form) { const fd = new FormData(form); await post(`/findings/${form.dataset.id}`, { status: fd.get("status"), note: fd.get("note") }); toast("Kaydedildi"); render(true); },
  async findFilter(el) { state.findFilter = el.dataset.v; render(); },
  async runScenario(el) { const jb = await post("/jobs", { type: "scenario", params: { name: el.dataset.name } }); toast(`İş kuyrukta (#${jb.job_id})`); location.hash = `#/is/${jb.job_id}`; },
};

/* ------------------------------------------------------------------ tema ve başlangıç */
function applyTheme(t) {
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
}
document.getElementById("themeBtn").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = cur === "dark" ? "light" : "dark";
  applyTheme(next); store.set("qa_theme", next);
});
applyTheme(store.get("qa_theme"));
state.token = store.get("qa_token");
render();
