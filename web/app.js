"use strict";
// Promo Finder front end: plain JS, no build step. Talks to server.py (/api/*) and polls run status.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const store = {
  get(k, d) { try { const v = localStorage.getItem("pf:" + k); return v === null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem("pf:" + k, JSON.stringify(v)); } catch { /* private mode */ } },
};

const S = { cfg: null, runId: null, timer: null, files: [], decisions: {}, reviewKey: null, last: null, qualityFor: null };

// ---------------------------------------------------------------- API
async function api(path, opts = {}) {
  const res = await fetch(path, opts);
  let body = null;
  try { body = await res.json(); } catch { /* not JSON */ }
  if (!res.ok) {
    const err = new Error((body && (body.error || body.detail)) || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return body;
}

// ---------------------------------------------------------------- settings
function settings() {
  const engine = $("input[name=engine]:checked").value;
  const sel = $("#model-select").value;
  return {
    engine,
    model: engine === "custom" ? (sel === "__custom" ? $("#custom-model").value.trim() : sel) : null,
    api_key: engine === "custom" ? $("#api-key").value.trim() : "",
    passcode: $("#passcode").value.trim() || null,
    fast: $("input[name=mode]:checked").value === "fast",
    duels: $("#duels").checked,
    repair_rounds: Number($("input[name=repair]:checked").value),
  };
}

function saveSettings() {
  const s = settings();
  store.set("settings", { engine: s.engine, model: $("#model-select").value, custom: $("#custom-model").value,
    fast: s.fast, duels: s.duels, repair: s.repair_rounds });
}

function restoreSettings() {
  const s = store.get("settings", null);
  if (!s) return;
  const eng = $(`input[name=engine][value=${s.engine}]`);
  if (eng) eng.checked = true;
  if (s.model && [...$("#model-select").options].some((o) => o.value === s.model)) $("#model-select").value = s.model;
  $("#custom-model").value = s.custom || "";
  $(`input[name=mode][value=${s.fast ? "fast" : "full"}]`).checked = true;
  $("#duels").checked = !!s.duels;
  const rep = $(`input[name=repair][value="${s.repair}"]`);
  if (rep) rep.checked = true;
}

function syncSettingsUI() {
  const engine = $("input[name=engine]:checked").value;
  $("#custom-box").hidden = engine !== "custom";
  $("#custom-model-wrap").hidden = $("#model-select").value !== "__custom";
  saveSettings();
}

// ---------------------------------------------------------------- tabs
function showTab(name) {
  $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
  ["run", "review", "quality"].forEach((t) => { $("#tab-" + t).hidden = t !== name; });
  if (name === "review") $("#review-dot").hidden = true;
  if (name === "quality" && S.runId) loadQuality();
}

// ---------------------------------------------------------------- uploads
function setFiles(list) {
  const max = S.cfg ? S.cfg.limits.max_files : 6;
  S.files = [...list].filter((f) => f.name.toLowerCase().endsWith(".txt")).slice(0, max);
  $("#file-list").innerHTML = S.files.map((f) => `<li>${esc(f.name)} · ${(f.size / 1000).toFixed(1)} KB</li>`).join("");
  $("#run-upload").disabled = !S.files.length;
}

// ---------------------------------------------------------------- run lifecycle
async function startRun(source) {
  const s = settings();
  const fd = new FormData();
  fd.append("settings", JSON.stringify({ ...s, source, title: $("#title").value.trim() }));
  if (source === "upload") S.files.forEach((f) => fd.append("files", f, f.name));
  setBusy(true);
  showStatus("running", "Starting run…");
  try {
    const { run_id } = await api("/api/runs", { method: "POST", body: fd });
    attach(run_id);
  } catch (e) {
    showStatus("error", e.message);
    setBusy(false);
  }
}

function attach(runId) {
  S.runId = runId;
  S.decisions = {};
  S.reviewKey = null;
  S.qualityFor = null;
  history.replaceState(null, "", "#run=" + runId);
  $("#progress").hidden = false;
  poll();
}

async function poll() {
  clearTimeout(S.timer);
  if (!S.runId) return;
  let snap;
  try {
    snap = await api(`/api/runs/${S.runId}`);
  } catch (e) {
    showStatus("error", e.status === 404 ? e.message : `Lost contact with the server (${e.message}). Retrying…`);
    if (e.status !== 404) S.timer = setTimeout(poll, 4000);
    else setBusy(false);
    return;
  }
  S.last = snap;
  renderProgress(snap);
  if (snap.status === "queued" || snap.status === "running") {
    setBusy(true);
    S.timer = setTimeout(poll, 1500);
    return;
  }
  setBusy(false);
  if (snap.status === "review") renderReview(snap.review);
  if (snap.status === "done") { renderReviewDone(); loadQuality(); }
}

function setBusy(b) {
  $("#run-sample").disabled = b;
  $("#run-upload").disabled = b || !S.files.length;
}

function showStatus(kind, html) {
  const el = $("#status");
  el.hidden = false;
  el.className = "status " + kind;
  el.innerHTML = html;
}

const STAGE_LABEL = { parse: "parse", bible: "show bible", mine: "mine", link: "link arcs", assemble: "assemble",
  refine: "judge + repair", coverage: "coverage", select: "select", duel: "duels", order: "rank", write: "write", editor: "editor" };

function renderProgress(s) {
  $("#stages").innerHTML = s.stages.map((st) => {
    const n = st.started > 1 ? ` ${st.finished}/${st.started}` : "";
    return `<li class="${st.state}">${esc(STAGE_LABEL[st.name] || st.name)}${esc(n)}</li>`;
  }).join("");
  $("#log").innerHTML = s.log.map((l) => `<li><span>${l.t.toFixed(1)}s</span>${esc(l.msg)}</li>`).join("");
  const logEl = $("#log");
  logEl.scrollTop = logEl.scrollHeight;
  $("#models").innerHTML = s.models.map((m) => `<div>${esc(m)}</div>`).join("");
  const calls = s.llm && s.llm.calls ? ` · ${s.llm.calls} API calls` : "";
  const hits = s.llm && s.llm.cache_hits ? ` · ${s.llm.cache_hits} cache hits` : "";
  const cost = s.llm && s.llm.cost_usd ? ` · ~$${s.llm.cost_usd.toFixed(3)}` : "";
  $("#elapsed").textContent = `${s.elapsed_s.toFixed(0)} s${calls}${hits}${cost}`;
  const msg = {
    queued: "Queued: waiting for a free worker…",
    running: "Running the graph… you can switch tabs; progress keeps updating.",
    review: `Editor review ready (round ${(s.review ? s.review.round : 0) + 1}). <button class="btn small primary" data-go="review">Open review</button>`,
    done: `Done. <button class="btn small" data-go="quality">See quality &amp; downloads</button>`,
    error: "Stopped: " + esc(s.error || "unknown error"),
  }[s.status];
  showStatus(s.status, msg);
}

// ---------------------------------------------------------------- review
function fmtSeg(seg) { return `E${String(seg.episode).padStart(2, "0")} ${seg.start} → ${seg.end}`; }

function scoreRows(sc) {
  if (!sc) return "";
  const rows = [["Hook", sc.hook], ["Cliff", sc.cliff], ["Standalone", sc.standalone], ["Trope", sc.trope], ["Pace", sc.pace]];
  return `<div class="scores">${rows.map(([k, v]) => `<span>${k}</span><div class="bar"><i style="width:${Math.max(0, Math.min(100, ((v || 0) / 5) * 100))}%"></i></div><span>${(v ?? 0).toFixed(1)}</span>`).join("")}</div>`;
}

function renderReview(review) {
  if (!review) return;
  const key = `${S.runId}:${review.round}`;
  $("#review-empty").hidden = true;
  $("#review-box").hidden = false;
  $("#resume").disabled = false;
  if (S.reviewKey === key) return; // keep the editor's half-made decisions across polls
  S.reviewKey = key;
  S.decisions = {};
  $("#review-title").textContent = `Editor review · round ${review.round + 1} · ${review.shortlist.length} promos · ${review.reserve} in reserve`;
  $("#cards").innerHTML = review.shortlist.map((p) => {
    const hookOk = p.hook_at_s != null && p.hook_at_s <= 3;
    const badges = [
      `<span class="badge accent">${esc(p.template)}</span>`,
      `<span class="badge">${p.duration_s.toFixed(1)} s</span>`,
      p.hook_at_s != null ? `<span class="badge ${hookOk ? "good" : "warn"}">hook @ ${p.hook_at_s.toFixed(1)} s</span>` : "",
      p.trope ? `<span class="badge">${esc(p.trope)}</span>` : "",
      p.in_free_window ? `<span class="badge good">free window</span>` : "",
      p.spoiler ? `<span class="badge warn">spoiler</span>` : "",
      ...(p.repairs || []).map((r) => `<span class="badge">repaired: ${esc(r)}</span>`),
      ...(p.flags || []).map((f) => `<span class="badge warn">${esc(f)}</span>`),
    ].join("");
    const nudgeRows = p.segments.map((s) => `<div class="nudge-row"><span>seg ${s.seg}</span>
        <input data-nudge="${esc(p.cand_id)}" data-seg="${s.seg}" data-side="start" placeholder="start ${esc(s.start)} or -0.5">
        <input data-nudge="${esc(p.cand_id)}" data-seg="${s.seg}" data-side="end" placeholder="end ${esc(s.end)} or +1.0"></div>`).join("");
    return `<article class="card promo" data-cand="${esc(p.cand_id)}">
      <div class="promo-top"><span class="rank">${esc(p.promo_id)}</span><span class="badge">final ${(p.final_score ?? 0).toFixed(2)}</span></div>
      <div class="badges">${badges}</div>
      <div class="segs">${p.segments.map((s) => `<span>${esc(fmtSeg(s))}</span>`).join("")}</div>
      <div class="beat"><b>Hook</b>${esc(p.hook)}</div>
      <div class="beat"><b>Cliffhanger</b>${esc(p.cliff)}</div>
      <div class="beat"><b>Open question</b>${esc(p.open_question || "-")}</div>
      <p class="why">${esc(p.why || "")}</p>
      ${scoreRows(p.scores)}
      <div class="actions">
        <button class="btn small" data-act="approve" aria-pressed="true">Approve</button>
        <button class="btn small rej" data-act="reject" aria-pressed="false">Reject</button>
        <button class="btn small" data-act="nudge" aria-pressed="false">Nudge</button>
      </div>
      <div class="extra" data-extra="reject" hidden><input data-reason="${esc(p.cand_id)}" placeholder="Reason (e.g. hook too slow, spoils Ep 3)"></div>
      <div class="extra" data-extra="nudge" hidden>${nudgeRows}<small class="hint">Absolute time (01:02.50) or a shift in seconds (-0.5, +1).</small></div>
    </article>`;
  }).join("");
  $("#review-dot").hidden = !$("#tab-review").hidden ? true : false;
}

function renderReviewDone() {
  if (!$("#review-box").hidden) {
    $("#resume").disabled = true;
    $("#review-title").textContent = "Review finished: final ranking written (see Quality & Exports).";
  }
}

function setAction(card, act) {
  $$(".actions .btn", card).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.act === act)));
  $$(".extra", card).forEach((x) => { x.hidden = x.dataset.extra !== act; });
  card.classList.toggle("rejected", act === "reject");
  card.classList.toggle("nudged", act === "nudge");
  S.decisions[card.dataset.cand] = act;
}

function collectDecisions() {
  const out = [];
  for (const card of $$("#cards .promo")) {
    const id = card.dataset.cand;
    const act = S.decisions[id] || "approve";
    if (act === "reject") {
      out.push({ cand_id: id, action: "reject", reason: $(`[data-reason="${CSS.escape(id)}"]`, card).value.trim() || "editor rejected" });
    } else if (act === "nudge") {
      const changes = {};
      for (const inp of $$(`[data-nudge="${CSS.escape(id)}"]`, card)) {
        const v = inp.value.trim();
        if (!v) continue;
        const seg = Number(inp.dataset.seg);
        changes[seg] = changes[seg] || { seg };
        changes[seg][inp.dataset.side] = v.includes(":") ? v : Number(v.replace(/^\+/, ""));
      }
      const list = Object.values(changes).filter((c) => ["start", "end"].every((k) => c[k] === undefined || typeof c[k] === "string" || Number.isFinite(c[k])));
      if (list.length) out.push({ cand_id: id, action: "nudge", changes: list });
    }
  }
  return out;
}

async function resume() {
  const decisions = collectDecisions();
  $("#resume").disabled = true;
  try {
    await api(`/api/runs/${S.runId}/resume`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ decisions }) });
    S.reviewKey = null;
    $("#review-title").textContent = "Resuming graph: backfilling and re-ranking…";
    showTab("run");
    poll();
  } catch (e) {
    $("#resume").disabled = false;
    alert(e.message);
  }
}

// ---------------------------------------------------------------- quality
const FILE_NOTES = { "ranked.json": "final promos + reserve", "candidates.jsonl": "every judged candidate", "report.md": "human-readable report",
  "run_meta.json": "models, calls, cost", "decisions.jsonl": "editor decisions log", "bible.json": "show bible" };

async function loadQuality() {
  if (!S.runId || !S.last || !S.last.files.includes("ranked.json")) return;
  const key = `${S.runId}:${S.last.status}:${S.last.files.length}:${S.last.review ? S.last.review.round : "x"}`;
  if (S.qualityFor === key) return;
  S.qualityFor = key;
  let q;
  try { q = await api(`/api/runs/${S.runId}/quality`); } catch { return; }
  $("#quality-empty").hidden = true;
  $("#quality-box").hidden = false;
  const pct = (v) => (v == null ? "–" : `${Math.round(v * 100)}%`);
  const llm = q.llm || {};
  const tiles = [
    [q.pool, "candidates assembled"], [q.rejected_by_code, "rejected by code validators"], [q.judged, "judged cold"],
    [pct(q.pass_rate), "eligible after judge + repair"], [`${q.repaired}`, "candidates repaired"], [pct(q.repair_yield), "repair yield"],
    [llm.calls ?? 0, "API calls"], [llm.cache_hits ?? 0, "cache hits"], [llm.cost_usd != null ? `$${llm.cost_usd.toFixed(3)}` : "$0", "est. cost"],
  ];
  $("#tiles").innerHTML = tiles.map(([v, k]) => `<div class="tile"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div></div>`).join("");
  const fails = Object.entries(q.failures || {}).sort((a, b) => b[1] - a[1]);
  const max = Math.max(1, ...fails.map(([, n]) => n));
  $("#fails").innerHTML = fails.map(([k, n]) => `<span>${esc(k)}</span><div class="bar"><i style="width:${(n / max) * 100}%"></i></div><span>${n}</span>`).join("") || "<span class='muted'>no judged candidates</span>";
  $("#downloads").innerHTML = S.last.files.map((f) => `<a href="/api/runs/${S.runId}/files/${f}" download>${esc(f)} <small>${esc(FILE_NOTES[f] || "")}</small></a>`).join("");
  try {
    const md = await (await fetch(`/api/runs/${S.runId}/files/report.md`)).text();
    $("#report").innerHTML = miniMarkdown(md);
  } catch { /* report optional */ }
}

// Small, escape-first Markdown renderer for report.md (headings, lists, quotes, bold, italics, code).
function miniMarkdown(md) {
  const inline = (s) => esc(s).replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>").replace(/(^|\W)_([^_]+)_(?=\W|$)/g, "$1<i>$2</i>");
  const out = [];
  let list = null;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of md.split(/\r?\n/)) {
    const line = raw.replace(/\s+$/, "");
    let m;
    if (!line.trim()) { close(); continue; }
    if ((m = line.match(/^(#{1,3})\s+(.*)$/))) { close(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); continue; }
    if ((m = line.match(/^>\s?(.*)$/))) { close(); out.push(`<p class="muted">${inline(m[1])}</p>`); continue; }
    if ((m = line.match(/^\s*[-*]\s+(.*)$/)) || (m = line.match(/^\s*\d+\.\s+(.*)$/))) {
      const kind = /^\s*\d+\./.test(line) ? "ol" : "ul";
      if (list !== kind) { close(); out.push(`<${kind}>`); list = kind; }
      out.push(`<li>${inline(m[1])}</li>`);
      continue;
    }
    if (list && /^\s{2,}/.test(raw)) { out.push(`<div>${inline(line.trim())}</div>`); continue; }
    close();
    out.push(`<p>${inline(line)}</p>`);
  }
  close();
  return out.join("");
}

// ---------------------------------------------------------------- boot
async function boot() {
  const slow = setTimeout(() => { $("#wake").hidden = false; }, 2500);
  for (let i = 0; i < 40 && !S.cfg; i++) {
    try { S.cfg = await api("/api/config"); } catch { await new Promise((r) => setTimeout(r, 3000)); }
  }
  clearTimeout(slow);
  $("#wake").hidden = true;
  if (!S.cfg) { showStatus("error", "Could not reach the server. Reload the page in a minute."); return; }

  const sel = $("#model-select");
  sel.innerHTML = S.cfg.presets.map((p) => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join("") + `<option value="__custom">Custom LiteLLM string…</option>`;
  $("#sample-desc").textContent = `"${S.cfg.sample.title}": ${S.cfg.sample.episodes.length} invented episodes (no real show content). Runs on server models from a pre-recorded cache, so it is quick and free.`;
  $("#limits").textContent = `up to ${S.cfg.limits.max_files} episodes, ${S.cfg.limits.max_file_kb} KB each`;
  $("#server-note").textContent = S.cfg.server_keys ? "" : "This server has no API keys set: use the offline engine or your own key.";
  restoreSettings();
  syncSettingsUI();

  const t = location.hash.match(/tab=(run|review|quality)/); // read before attach() rewrites the hash
  const m = location.hash.match(/run=([a-f0-9]+)/);
  if (m) attach(m[1]);
  if (t) showTab(t[1]);
}

document.addEventListener("DOMContentLoaded", () => {
  $$("input[name=engine], input[name=mode], input[name=repair], #duels").forEach((el) => el.addEventListener("change", syncSettingsUI));
  $("#model-select").addEventListener("change", syncSettingsUI);
  $("#custom-model").addEventListener("input", saveSettings);
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
  $("#run-sample").addEventListener("click", () => startRun("sample"));
  $("#run-upload").addEventListener("click", () => startRun("upload"));
  $("#files").addEventListener("change", (e) => setFiles(e.target.files));
  const drop = $("#drop");
  drop.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#files").click(); } });
  ["dragenter", "dragover"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add("over"); }));
  ["dragleave", "drop"].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
  drop.addEventListener("drop", (e) => setFiles(e.dataTransfer.files));
  $("#status").addEventListener("click", (e) => { const go = e.target.closest("[data-go]"); if (go) showTab(go.dataset.go); });
  $("#cards").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (b) setAction(b.closest(".promo"), b.dataset.act);
  });
  $("#resume").addEventListener("click", resume);
  boot();
});
