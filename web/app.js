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

// ---------------------------------------------------------------- API + feedback
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

function toast(msg) {
  const t = $("#toast");
  t.textContent = msg;
  t.hidden = false;
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { t.hidden = true; }, 6000);
}

// ---------------------------------------------------------------- theme
function currentTheme() {
  const set = document.documentElement.dataset.theme;
  if (set) return set;
  return matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
}
function toggleTheme() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  store.set("theme", next);
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
  $("#layout").classList.toggle("no-side", name !== "run"); // run settings only matter on the run tab
  if (name === "review") $("#review-dot").hidden = true;
  if (name === "quality" && S.runId) loadQuality();
}

// ---------------------------------------------------------------- uploads
function setFiles(list) {
  const max = S.cfg ? S.cfg.limits.max_files : 6;
  const all = [...list].filter((f) => f.name.toLowerCase().endsWith(".txt"));
  if (all.length > max) toast(`Only the first ${max} episodes are used in the hosted demo.`);
  S.files = all.slice(0, max);
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
    S.watching = true; // a sample replay can finish before the first poll ever sees it running
    attach(run_id);
  } catch (e) {
    showStatus("error", esc(e.message));
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
    showStatus("error", esc(e.status === 404 ? e.message : `Lost contact with the server (${e.message}). Retrying…`));
    if (e.status !== 404) S.timer = setTimeout(poll, 4000);
    else setBusy(false);
    return;
  }
  S.last = snap;
  renderProgress(snap);
  if (snap.status === "queued" || snap.status === "running") {
    S.watching = true;
    setBusy(true);
    S.timer = setTimeout(poll, 1500);
    return;
  }
  setBusy(false);
  if (snap.status === "review") renderReview(snap.review);
  if (snap.status === "done") { renderReviewDone(); loadQuality(); }
  autoAdvance(snap);
}

// Move the editor to the next step when a run this tab saw working reaches it (not when a finished run is reopened
// from a link), and only if they are still on the Run tab.
function autoAdvance(snap) {
  const target = snap.status === "review" ? "review" : snap.status === "done" ? "quality" : null;
  if (!target || !S.watching) return;
  S.watching = false;
  if (!$("#tab-run").hidden) {
    showTab(target);
    window.scrollTo({ top: 0, behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
  }
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

const STAGE_LABEL = { parse: "Parse", bible: "Bible", mine: "Mine", link: "Link arcs", assemble: "Assemble",
  refine: "Judge + repair", coverage: "Coverage", select: "Select", duel: "Duels", order: "Rank", write: "Write", editor: "Editor" };
// Plain-language line for the stage that is running now (also the stepper tooltip).
const STAGE_HELP = { parse: "Reading the screenplays line by line", bible: "Writing the show bible: characters, secrets, recurring tropes",
  mine: "Finding hooks, reveals and cliffhangers in each episode", link: "Linking set-ups to their pay-offs across episodes",
  assemble: "Cutting 20–60 s candidate promos and checking the rules in code", refine: "Scoring each cut cold and repairing the weak ones",
  coverage: "Making sure the shortlist covers different episodes and story arcs", select: "Choosing the shortlist and the reserve",
  duel: "Comparing finalists head-to-head", order: "Putting the promos in posting order", write: "Writing the report and output files",
  editor: "Waiting for you to review the shortlist" };

function renderProgress(s) {
  const lastActive = s.stages.reduce((k, st, i) => (st.state !== "pending" ? i : k), -1);
  $("#stages").innerHTML = s.stages.map((st, i) => {
    const skipped = st.state === "pending" && i < lastActive; // e.g. duels switched off
    const state = skipped ? "skipped" : st.state;
    const mark = state === "done" ? "✓" : state === "waiting" ? "❚❚" : skipped ? "–" : String(i + 1);
    const cnt = skipped ? "skipped" : st.started > 1 ? `${st.finished}/${st.started}` : "";
    return `<li class="${state}" title="${esc(STAGE_HELP[st.name] || "")}"><span class="node">${mark}</span><span>${esc(STAGE_LABEL[st.name] || st.name)}</span><span class="cnt">${esc(cnt)}</span></li>`;
  }).join("");
  const done = s.stages.filter((st) => st.state === "done").length;
  const cur = s.stages.find((st) => st.state === "running" || st.state === "waiting");
  $("#now").innerHTML = s.status === "running" || s.status === "queued"
    ? `<b>Step ${Math.min(done + 1, s.stages.length)} of ${s.stages.length}:</b> ${esc(cur ? (STAGE_HELP[cur.name] || STAGE_LABEL[cur.name] || cur.name) : "Starting")}…`
    : "";
  $("#log").innerHTML = s.log.map((l) => `<li><span>${l.t.toFixed(1)}s</span>${esc(l.msg)}</li>`).join("");
  const logEl = $("#log");
  logEl.scrollTop = logEl.scrollHeight;
  $("#models").innerHTML = s.models.map((m) => `<div>${esc(m)}</div>`).join("");
  renderRawPicker(s);
  $("#elapsed").innerHTML = runCostLine(s);
  const msg = {
    queued: "Queued: waiting for a free worker…",
    running: "Working on it. You can switch tabs or leave this page open; progress keeps updating.",
    review: `<span>Your shortlist is ready to review (round ${(s.review ? s.review.round : 0) + 1}).</span><button class="btn small primary" data-go="review">Review promos</button>`,
    done: `<span>Done. The final ranking is ready.</span><button class="btn small primary" data-go="quality">See results &amp; downloads</button>`,
    error: "Stopped: " + esc(s.error || "unknown error"),
  }[s.status];
  showStatus(s.status, msg);
}

const fmtDur = (sec) => (sec >= 60 ? `${Math.floor(sec / 60)}m${String(Math.round(sec % 60)).padStart(2, "0")}s` : `${sec.toFixed(sec < 10 ? 1 : 0)} s`);
const isReplay = (llm) => !!llm && !llm.calls && !!llm.cache_hits;

// One honest line about time and cost: a replay says so, and quotes the run that recorded the answers.
function runCostLine(s) {
  const llm = s.llm || {};
  if (isReplay(llm)) {
    const r = s.recorded;
    const orig = r ? ` · recorded run: <b>${r.calls} calls · ${fmtDur(r.seconds)} · $${r.cost_usd.toFixed(3)}</b>` : "";
    return `<span class="replay">Replayed ${llm.cache_hits} recorded answers (0 new calls)${orig}</span>`;
  }
  const parts = [fmtDur(s.elapsed_s || 0)];
  if (llm.calls) parts.push(`${llm.calls} API calls`);
  if (llm.cache_hits) parts.push(`${llm.cache_hits} cached`);
  if (llm.cost_usd) parts.push(`~$${llm.cost_usd.toFixed(3)}`);
  if (llm.offline) parts.push("offline, no API");
  return esc(parts.join(" · "));
}

// ---------------------------------------------------------------- raw outputs
// Shows each output file exactly as the pipeline wrote it (no formatting), fetched only while the panel is open.
const RAW_MAX_CHARS = 400_000;

function renderRawPicker(s) {
  const sel = $("#raw-file");
  const want = sel.value || store.get("rawFile", "ranked.json");
  const sig = s.files.join("|");
  if (sel.dataset.sig !== sig) {
    sel.dataset.sig = sig;
    sel.innerHTML = s.files.map((f) => `<option value="${esc(f)}">${esc(f)}${FILE_NOTES[f] ? ` · ${esc(FILE_NOTES[f])}` : ""}</option>`).join("");
    if (s.files.includes(want)) sel.value = want;
  }
  if ($("#raw-wrap").open) loadRaw();
}

async function loadRaw(force = false) {
  const name = $("#raw-file").value;
  const pre = $("#raw");
  const dl = $("#raw-dl");
  if (!S.runId || !name) {
    pre.textContent = "No output files yet. They appear as each stage writes them.";
    $("#raw-meta").textContent = "";
    dl.hidden = true;
    return;
  }
  // refetch only when the file, the run or its progress changed: a running graph appends to the .jsonl files
  const s = S.last || {};
  const key = `${S.runId}:${name}:${s.status}:${s.log ? s.log.length : 0}:${s.review ? s.review.round : "x"}`;
  if (!force && loadRaw.key === key) return;
  loadRaw.key = key;
  const url = `/api/runs/${S.runId}/files/${encodeURIComponent(name)}`;
  dl.href = url;
  dl.hidden = false;
  try {
    const res = await fetch(url);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const text = await res.text();
    if ($("#raw-file").value !== name) return; // the editor picked another file meanwhile
    const lines = name.endsWith(".jsonl") ? text.split("\n").filter((l) => l.trim()).length : null;
    $("#raw-meta").textContent = [`${(new Blob([text]).size / 1000).toFixed(1)} KB`, lines != null ? `${lines} rows` : ""].filter(Boolean).join(" · ");
    pre.textContent = !text ? "(empty file)"
      : text.length > RAW_MAX_CHARS ? text.slice(0, RAW_MAX_CHARS) + `\n\n… truncated at ${RAW_MAX_CHARS.toLocaleString()} characters. Download the file for the rest.`
        : text;
  } catch (e) {
    loadRaw.key = null;
    pre.textContent = `Could not load ${name} (${e.message}).`;
  }
}

// ---------------------------------------------------------------- review cards
const TEMPLATE_LABEL = { continuous: "Continuous", cold_open: "Cold open", bridge: "Episode bridge", arc_stitch: "Arc stitch" };
const toSec = (ts) => String(ts).split(":").reduce((a, p) => a * 60 + Number(p), 0);
const fmtSeg = (seg) => `E${String(seg.episode).padStart(2, "0")}  ${seg.start} → ${seg.end}`;

function clipStrip(p) {
  const lens = p.segments.map((s) => Math.max(0.1, toSec(s.end) - toSec(s.start)));
  const total = lens.reduce((a, b) => a + b, 0) || 1;
  const segs = p.segments.map((s, i) => `<span class="s" style="flex:${lens[i]}" title="${esc(fmtSeg(s))}">E${String(s.episode).padStart(2, "0")} · ${lens[i].toFixed(0)}s</span>`).join("");
  const hook = p.hook_at_s != null ? `<i class="hook" style="left:${Math.min(98, (p.hook_at_s / total) * 100)}%" title="hook at ${p.hook_at_s.toFixed(1)} s"></i>` : "";
  return `<div class="strip" aria-label="Clip structure">${segs}${hook}<i class="cliff" title="cliffhanger at the cut"></i></div>`;
}

function rings(sc) {
  if (!sc) return "";
  const rows = [["Hook", sc.hook, "How fast the opening grabs attention"], ["Cliff", sc.cliff, "How strongly the ending makes you want the next episode"],
    ["Alone", sc.standalone, "Makes sense to someone who has never seen the show"], ["Trope", sc.trope, "How clearly it sells a popular story trope"],
    ["Pace", sc.pace, "No slow or dead moments"]];
  return `<div class="scores">${rows.map(([k, v, help]) => `<div class="score" title="${esc(help)}: ${(v ?? 0).toFixed(1)} / 5"><div class="ring" style="--p:${Math.max(0, Math.min(100, ((v || 0) / 5) * 100))}"><span>${(v ?? 0).toFixed(1)}</span></div>${k}</div>`).join("")}</div>`;
}

function renderReview(review) {
  if (!review) return;
  const key = `${S.runId}:${review.round}`;
  $("#review-empty").hidden = true;
  $("#review-box").hidden = false;
  $("#resume").disabled = false;
  if ($("#tab-review").hidden) $("#review-dot").hidden = false;
  if (S.reviewKey === key) return; // keep the editor's half-made decisions across polls
  S.reviewKey = key;
  S.decisions = {};
  S.reserve = review.reserve;
  $("#review-title").textContent = `Round ${review.round + 1} · ${review.shortlist.length} promos · ${review.reserve} in reserve`;
  const held = Object.entries(review.held_back || {}).filter(([r]) => r !== "rejected by the editor");
  const heldTxt = held.length ? ` ${held.reduce((a, [, n]) => a + n, 0)} other eligible promo(s) were held back: ${held.map(([r, n]) => `${n} ${r}`).join(", ")}.` : "";
  const note = $("#reserve-note");
  note.hidden = !(review.reserve === 0 || heldTxt);
  note.textContent = review.reserve === 0
    ? `No reserve left: rejecting a promo removes it, with no replacement.${heldTxt}`
    : `${review.reserve} promo(s) in reserve will replace rejects, best first.${heldTxt}`;
  const types = [...new Set(review.shortlist.map((p) => p.template))];
  $("#legend").innerHTML = "<span>Card colour = promo type:</span>" + types.map((t) => `<span><i style="--c:var(--t-${/^[a-z_]+$/.test(t) ? t : "continuous"})"></i>${esc(TEMPLATE_LABEL[t] || t)}</span>`).join("");
  $("#cards").innerHTML = review.shortlist.map((p) => {
    const hookOk = p.hook_at_s != null && p.hook_at_s <= 3;
    const tc = `var(--t-${/^[a-z_]+$/.test(p.template) ? p.template : "continuous"}, var(--accent))`;
    const badges = [
      p.trope ? `<span class="badge">${esc(p.trope.replace(/_/g, " "))}</span>` : "",
      p.in_free_window ? `<span class="badge good">free-episode window</span>` : "",
      p.spoiler ? `<span class="badge warn">spoiler</span>` : "",
      ...(p.repairs || []).map((r) => `<span class="badge">repaired: ${esc(r.replace(/_/g, " "))}</span>`),
      ...(p.flags || []).map((f) => `<span class="badge warn">${esc(f)}</span>`),
    ].join("");
    const nudgeRows = p.segments.map((s) => `<div class="nudge-row"><span>seg ${s.seg}</span>
        <input data-nudge="${esc(p.cand_id)}" data-seg="${s.seg}" data-side="start" placeholder="start ${esc(s.start)}" aria-label="New start for segment ${s.seg}">
        <input data-nudge="${esc(p.cand_id)}" data-seg="${s.seg}" data-side="end" placeholder="end ${esc(s.end)}" aria-label="New end for segment ${s.seg}"></div>`).join("");
    return `<article class="promo pending" data-cand="${esc(p.cand_id)}" style="--tc:${tc}">
      <div class="cover">
        <div class="cover-top">
          <div><div class="rank">#${p.rank}<small>${esc(p.promo_id)}</small></div>${p.rank === 1 ? `<span class="ribbon">Post first</span>` : ""}</div>
          <span class="score-pill">${(p.final_score ?? 0).toFixed(2)}<small> / 5</small></span>
        </div>
        <div class="cover-meta">
          <span>${esc(TEMPLATE_LABEL[p.template] || p.template)}</span>
          <span>${p.duration_s.toFixed(1)} s</span>
          ${p.hook_at_s != null ? `<span>${hookOk ? "⚡" : "⚠"} hook at ${p.hook_at_s.toFixed(1)} s</span>` : ""}
        </div>
        ${clipStrip(p)}
      </div>
      <div class="promo-body">
        <div class="segs">${p.segments.map((s) => `<span>${esc(fmtSeg(s))}</span>`).join("")}</div>
        <p class="line"><b><span>Hook</span></b>${esc(p.hook)}</p>
        <p class="line cliff"><b><span>Cliffhanger</span></b>${esc(p.cliff)}</p>
        <p class="question">${esc(p.open_question || "-")}</p>
        ${p.why ? `<p class="why">${esc(p.why)}</p>` : ""}
        ${badges ? `<div class="badges">${badges}</div>` : ""}
        ${rings(p.scores)}
        <div class="actions" role="group" aria-label="Decision for ${esc(p.promo_id)}">
          <button type="button" data-act="approve" aria-pressed="false">Approve</button>
          <button type="button" data-act="reject" aria-pressed="false">Reject</button>
          <button type="button" data-act="nudge" aria-pressed="false">Nudge</button>
        </div>
        <div class="extra" data-extra="reject" hidden><input data-reason="${esc(p.cand_id)}" placeholder="Why? e.g. hook too slow, spoils Ep 3" aria-label="Reject reason"></div>
        <div class="extra" data-extra="nudge" hidden>${nudgeRows}<small class="hint">Absolute time (01:02.50) or a shift in seconds (-0.5, +1).</small></div>
      </div>
    </article>`;
  }).join("");
  updateDecisionSummary();
}

function renderReviewDone() {
  if (!$("#review-box").hidden) {
    $("#resume").disabled = true;
    $("#approve-rest").disabled = true;
    $("#resume").textContent = "Finalized";
    $("#review-title").textContent = "Review finished. The final ranking is in Results & downloads.";
    $("#decision-sum").textContent = "";
    $("#reserve-note").hidden = true;
  }
}

function setAction(card, act) {
  $$(".actions button", card).forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.act === act)));
  $$(".extra", card).forEach((x) => { x.hidden = x.dataset.extra !== act; });
  card.classList.remove("pending");
  card.classList.toggle("rejected", act === "reject");
  card.classList.toggle("nudged", act === "nudge");
  S.decisions[card.dataset.cand] = act;
  updateDecisionSummary();
  const focus = $(`.extra[data-extra="${act}"] input`, card);
  if (focus) focus.focus();
}

function updateDecisionSummary() {
  const acts = $$("#cards .promo").map((c) => S.decisions[c.dataset.cand] || "pending");
  const n = (a) => acts.filter((x) => x === a).length;
  const pending = n("pending"), rej = n("reject"), keep = n("approve") + n("nudge");
  $("#decision-sum").innerHTML = [
    pending ? `<span class="pending">${pending} to decide</span>` : "",
    `<span class="approve">${n("approve")} approved</span>`, `<span class="reject">${rej} rejected</span>`,
    `<span class="nudge">${n("nudge")} nudged</span>`].join("");
  const btn = $("#resume");
  btn.disabled = pending > 0 || !acts.length;
  btn.textContent = pending > 0 ? `Decide all ${acts.length} to finish`
    : rej === 0 ? `Finalize ${keep} promo${keep === 1 ? "" : "s"}`
      : (S.reserve || 0) > 0 ? `Replace ${rej} from reserve & re-rank`
        : `Finalize ${keep} (${rej} removed, no replacement)`;
  $("#approve-rest").disabled = pending === 0;
}

function approveRest() {
  $$("#cards .promo").filter((c) => !S.decisions[c.dataset.cand]).forEach((c) => setAction(c, "approve"));
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
    S.watching = true;
    $("#review-title").textContent = "Resuming: backfilling and re-ranking…";
    showTab("run");
    poll();
  } catch (e) {
    $("#resume").disabled = false;
    toast(e.message);
  }
}

// ---------------------------------------------------------------- quality
const FILE_NOTES = { "ranked.json": "final promos + reserve", "candidates.jsonl": "every judged candidate", "report.md": "readable report",
  "run_meta.json": "models, calls, cost", "decisions.jsonl": "editor decisions", "bible.json": "show bible",
  "beats.jsonl": "raw mining + linking output", "pool.jsonl": "assembled candidates", "rejected_by_code.jsonl": "cuts that failed the code checks",
  "duels.jsonl": "head-to-head results", "features.json": "per-episode heat", "llm_calls.jsonl": "every model call: model, tokens, time" };

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
  const rec = q.recorded;
  const replay = isReplay(llm);
  const costTiles = replay && rec
    ? [[rec.calls, "API calls", "in the recorded run; this replay made 0"], [`$${rec.cost_usd.toFixed(3)}`, "cost", "of the recorded run; this replay was free"],
       [fmtDur(rec.seconds), "run time", "of the recorded run"]]
    : replay
      ? [[0, "new API calls", `${llm.cache_hits} answers replayed from cache`], ["$0", "cost of this replay", ""]]
      : llm.offline ? [["offline", "engine", "no API: keyword heuristics"]]
        : [[llm.calls ?? 0, "API calls", llm.cache_hits ? `+ ${llm.cache_hits} cached` : ""], [`$${(llm.cost_usd || 0).toFixed(3)}`, "estimated cost", ""],
           [fmtDur(q.seconds || 0), "run time", ""]];
  const tiles = [
    [pct(q.pass_rate), "eligible after judge + repair", "", true], [pct(q.repair_yield), "repair yield", `${q.repaired} repaired`, true],
    [q.pool, "candidates assembled", `${q.rejected_by_code} more rejected by code`], [q.judged, "judged cold", ""],
    [q.reserve ?? 0, "in reserve", "promoted if you reject"],
    ...costTiles.map(([v, k, sub]) => [v, k, sub]),
  ];
  $("#tiles").innerHTML = tiles.map(([v, k, sub, hl]) => `<div class="tile${hl ? " hl" : ""}"><div class="v">${esc(v)}</div><div class="k">${esc(k)}</div>${sub ? `<div class="sub">${esc(sub)}</div>` : ""}</div>`).join("");
  // clean passes first and green; every failure type in red
  const fails = Object.entries(q.failures || {}).sort((a, b) => (a[0] === "clean pass" ? -1 : b[0] === "clean pass" ? 1 : b[1] - a[1]));
  const max = Math.max(1, ...fails.map(([, n]) => n));
  $("#fails").innerHTML = fails.map(([k, n]) => {
    const ok = k === "clean pass" || k === "none";
    return `<span>${esc(ok ? "Clean pass" : k.replace(/_/g, " "))}</span><div class="bar"><i class="${ok ? "good" : "bad"}" style="width:${(n / max) * 100}%"></i></div><span>${n}</span>`;
  }).join("") || "<span class='meta'>No judged candidates.</span>";
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
    if ((m = line.match(/^>\s?(.*)$/))) { close(); out.push(`<p class="meta">${inline(m[1])}</p>`); continue; }
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
  $("#sample-title").textContent = S.cfg.sample.title || "Sample show";
  $("#sample-desc").textContent = `${S.cfg.sample.episodes.length} invented episodes, no real show content. On server models it replays recorded answers, so it finishes in about a second and costs nothing.`;
  $("#limits").textContent = `Up to ${S.cfg.limits.max_files} episodes, ${S.cfg.limits.max_file_kb} KB each`;
  $("#server-note").textContent = S.cfg.server_keys ? "" : "This server has no API keys set: use the offline engine or your own key.";
  restoreSettings();
  syncSettingsUI();

  const t = location.hash.match(/tab=(run|review|quality)/); // read before attach() rewrites the hash
  const m = location.hash.match(/run=([a-f0-9]+)/);
  if (m) attach(m[1]);
  if (t) showTab(t[1]);
  // "Try the sample show" on the landing page links to app.html#sample: start it straight away.
  if (!m && /^#sample\b/.test(location.hash)) {
    history.replaceState(null, "", location.pathname);
    startRun("sample");
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $$("input[name=engine], input[name=mode], input[name=repair], #duels").forEach((el) => el.addEventListener("change", syncSettingsUI));
  $("#model-select").addEventListener("change", syncSettingsUI);
  $("#custom-model").addEventListener("input", saveSettings);
  $("#theme").addEventListener("click", toggleTheme);
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
  $$("[data-start]").forEach((b) => b.addEventListener("click", () => { showTab("run"); startRun(b.dataset.start); }));
  $("#cards").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]");
    if (b) setAction(b.closest(".promo"), b.dataset.act);
  });
  $("#raw-wrap").addEventListener("toggle", () => { if ($("#raw-wrap").open) loadRaw(); });
  $("#raw-file").addEventListener("change", () => { store.set("rawFile", $("#raw-file").value); loadRaw(true); });
  $("#resume").addEventListener("click", resume);
  $("#approve-rest").addEventListener("click", approveRest);
  boot();
});
