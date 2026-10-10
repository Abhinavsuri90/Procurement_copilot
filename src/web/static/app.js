// Procurement Request Copilot - single-page UI (vanilla JS, no build step).
"use strict";

const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[c]));
const money = (v) => (v === null || v === undefined || v === "") ? "—" : "$" + Number(v).toLocaleString("en-US", {maximumFractionDigits: 2});

const REC_LABEL = {
  recommend_approve: "Recommend approval", use_existing_tool: "Use existing tool", request_more_info: "Request more info",
  escalate_to_human: "Escalate to human", recommend_reject: "Recommend rejection",
};
const state = {requests: [], selected: null, detail: null, run: null, trace: [], health: null};

async function api(path, opts = {}) {
  const res = await fetch(path, {headers: {"Content-Type": "application/json"}, ...opts});
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail ? (typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail)) : res.statusText);
  return body;
}

// ---------------------------------------------------------------- header / health
async function loadHealth() {
  try {
    const h = await api("/api/health");
    state.health = h;
    $("#vendor-dot").className = "dot " + (h.vendor_service.ok ? "ok" : "bad");
    $("#vendor-pill").title = `Vendor-risk service ${h.vendor_service.ok ? "healthy" : "unreachable"} (${h.vendor_service.url})`;
    $("#model-pill").textContent = h.llm.configured ? `${h.llm.model} · ${h.llm.mode}` : "no API key · replay / rules";
    $("#model-pill").className = "pill " + (h.llm.configured ? "" : "warn");
    $("#reviewer-role").innerHTML = h.roles.map((r) => `<option>${esc(r)}</option>`).join("");
  } catch (e) {
    $("#vendor-dot").className = "dot bad";
  }
}

// ---------------------------------------------------------------- queue
async function loadQueue() {
  state.requests = await api("/api/requests");
  renderQueue();
}

function renderQueue() {
  const q = $("#search").value.toLowerCase();
  const f = $("#status-filter").value;
  const items = state.requests.filter((r) =>
    (!f || r.status === f) &&
    (!q || [r.request_id, r.product_name, r.vendor_name, r.category].join(" ").toLowerCase().includes(q)));
  $("#queue-list").innerHTML = items.length ? items.map((r) => `
    <li><button class="queue-item ${state.selected === r.request_id ? "active" : ""}" data-id="${esc(r.request_id)}">
      <span class="qi-top"><b>${esc(r.request_id)}</b><span class="chip ${statusClass(r.status)}">${esc(r.status)}</span></span>
      <span class="qi-name">${esc(r.product_name)}</span>
      <span class="qi-meta">${esc(r.vendor_name)} · ${money(r.annual_cost_usd)}${r.recommendation ? " · " + esc(REC_LABEL[r.recommendation] || r.recommendation) : ""}</span>
    </button></li>`).join("") : `<li class="empty small">No requests match.</li>`;
}

const statusClass = (s) => ({"Approved": "ok", "Rejected": "bad", "Awaiting human": "warn", "Escalated": "warn",
  "Info requested": "info", "Analyzed": "info", "Partially approved": "info"}[s] || "");

// ---------------------------------------------------------------- request detail
async function selectRequest(id) {
  if (state.selected !== id) $("#live").hidden = true;
  state.selected = id;
  if (location.hash !== "#" + id) history.replaceState(null, "", "#" + encodeURIComponent(id));
  renderQueue();
  state.detail = await api(`/api/requests/${encodeURIComponent(id)}`);
  state.run = state.detail.latest_run;
  state.trace = [];
  if (state.run) {
    const full = await api(`/api/runs/${state.run.run_id}`);
    state.trace = full.trace || [];
  }
  renderDetail();
  renderDecision();
  setWorkflow(state.run ? (["Approved", "Rejected", "Info requested", "Escalated"].includes(state.detail.status) ? "done" : "human") : "request");
}

function renderDetail() {
  const r = state.detail.request;
  $("#empty-centre").hidden = true;
  $("#request-pane").hidden = false;
  $("#req-title").textContent = `${r.request_id} · ${r.product_name ?? "(no product)"}`;
  $("#req-status").textContent = state.detail.status;
  $("#req-status").className = "chip " + statusClass(state.detail.status);
  const row = (k, v) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`;
  $("#tab-details").innerHTML = `<table class="kv">
    ${row("Requester", esc(r.requester_id))}
    ${row("Vendor", esc(r.vendor_name))}
    ${row("Category", esc(r.category))}
    ${row("Annual cost", money(r.annual_cost_usd))}
    ${row("Users", esc(r.user_count ?? "—"))}
    ${row("Data access", esc(r.data_access_level ?? "—"))}
    ${row("Integrations", esc((r.requested_integrations || []).join(", ") || "none"))}
    ${row("Urgency", esc(r.urgency ?? "—"))}
    ${row("Justification", `<span class="untrusted" title="Untrusted business data - never treated as instructions">${esc(r.business_justification ?? "—")}</span>`)}
  </table>`;
  renderTrace();
  renderAudit();
}

function renderTrace() {
  if (!state.trace.length) { $("#tab-trace").innerHTML = `<p class="empty small">No run yet.</p>`; return; }
  $("#tab-trace").innerHTML = `<table class="trace"><thead><tr><th>#</th><th>Call</th><th>By</th><th>Status</th><th>Latency</th><th>Tokens</th></tr></thead><tbody>` +
    state.trace.map((ev, i) => {
      const p = ev.payload;
      if (ev.kind === "llm") return `<tr><td>${i + 1}</td><td><b>LLM</b> ${esc(p.call_id)} → ${esc((p.tool_calls || []).join(", ") || "text")}</td><td>${esc(p.agent)}</td><td>${p.ok ? "ok" : "error"}${p.replayed ? " (replayed)" : ""}</td><td>${Math.round(p.latency_ms)} ms</td><td>${p.tokens_in}/${p.tokens_out}</td></tr>`;
      if (ev.kind === "tool") return `<tr><td>${i + 1}</td><td><button class="link" data-call="${esc(p.call_id)}">${esc(p.call_id)} ${esc(p.tool)}</button> <code>${esc(JSON.stringify(p.args))}</code></td><td>${esc(p.caller)}</td><td>${p.ok ? "ok" : "<span class='bad-t'>" + esc(p.error?.code) + "</span>"}</td><td>${Math.round(p.latency_ms)} ms</td><td></td></tr>`;
      return `<tr><td>${i + 1}</td><td colspan="5">${esc(p.message)}</td></tr>`;
    }).join("") + `</tbody></table>`;
}

function renderAudit() {
  const d = state.detail;
  const events = [
    ...d.runs.map((r) => ({t: r.created_at, html: `Copilot run <code>${esc(r.run_id)}</code> (${esc(r.architecture)}) → <b>${esc(REC_LABEL[r.recommendation] || r.recommendation)}</b>`})),
    ...d.audit.map((a) => ({t: a.created_at, html: `<b>${esc(a.reviewer_role)}</b>: ${esc(a.action)}${a.is_override ? " <span class='chip warn'>override</span>" : ""}${a.is_exception ? " <span class='chip bad'>exception</span>" : ""} — AI said ${esc(a.ai_recommendation)} (${esc(a.architecture)}, ${esc(a.model)})${a.reason ? `<br><i>${esc(a.reason)}</i>` : ""}`})),
  ].sort((a, b) => a.t.localeCompare(b.t));
  $("#tab-audit").innerHTML = events.length ? `<ol class="timeline">${events.map((e) => `<li><time>${esc(e.t)}</time> ${e.html}</li>`).join("")}</ol>` : `<p class="empty small">No activity yet.</p>`;
}

// ---------------------------------------------------------------- decision panel
function renderDecision() {
  const run = state.run;
  $("#decision-empty").hidden = !!run;
  $("#decision-body").hidden = !run;
  $("#human-form").hidden = !run;
  if (!run) return;
  const d = run.decision;
  const m = d.meta;
  const toolOut = (id) => state.trace.find((e) => e.kind === "tool" && e.payload.call_id === id);
  const degraded = d.risk_flags.some((f) => f.code === "vendor_risk_unavailable");
  const banners = [];
  if (m.deterministic_only && m.architecture !== "R-rules-only") banners.push(`<div class="banner warn">AI unavailable — deterministic checks only. ${esc((m.warnings || []).join("; "))}</div>`);
  if (m.replayed) banners.push(`<div class="banner info">Replayed from the recorded evaluation run (no API key needed).</div>`);
  if (degraded) banners.push(`<div class="banner warn">Vendor service unavailable — recommendation limited to escalation.</div>`);
  if (d.overrides.length) banners.push(`<div class="banner bad">Guardrails overrode the agent: ${d.overrides.map((o) => `${esc(o.agent_value)} → ${esc(o.final_value)} (${esc(o.reason)})`).join("; ")}</div>`);
  const g = m.guardrails || {};
  $("#decision-body").innerHTML = `
    ${banners.join("")}
    <div class="rec rec-${esc(d.recommendation)}">${esc(REC_LABEL[d.recommendation] || d.recommendation)}</div>
    <p class="summary">${esc(d.summary)}</p>
    <div class="meta-line">${esc(m.architecture)} · ${esc(m.model)} · ${m.llm_calls} LLM / ${m.tool_calls} tool calls · ${Math.round(m.latency_ms)} ms${g.ungrounded_removed?.length ? ` · ${g.ungrounded_removed.length} ungrounded evidence item(s) removed` : ""}</div>

    <h3>Next step</h3>
    <p class="next"><span class="chip info">${esc(d.next_step.owner_role)}</span> <b>${esc(d.next_step.action)}</b> — ${esc(d.next_step.detail)}</p>
    ${d.human_handoff.required ? `<p class="handoff">Human handoff → <b>${esc(d.human_handoff.assigned_role)}</b>: ${esc(d.human_handoff.decision_needed)}<br><small>${[...new Set(d.human_handoff.reasons.map((r) => r.replace(/^agent: /, "")))].map(esc).join(" · ")}</small></p>` : ""}

    <h3>Approvals required (${d.approvals_required.length})</h3>
    <ul class="list">${d.approvals_required.map((a) => `<li><b>${esc(a.role)}</b> <span class="chip rule">${esc(a.rule_id)}</span> <span class="tag">${a.source === "agent" ? "agent" : "policy engine"}</span><br><small>${esc(a.reason)}</small></li>`).join("")}</ul>

    <h3>Risk flags (${d.risk_flags.length})</h3>
    <ul class="list">${d.risk_flags.map((f) => `<li><span class="sev sev-${esc(f.severity)}">${esc(f.severity)}</span> <b>${esc(f.code)}</b>${f.rule_id ? ` <span class="chip rule">${esc(f.rule_id)}</span>` : ""}${f.source === "agent" ? ` <span class="tag">agent</span>` : ""}<br><small>${esc(f.detail)}</small></li>`).join("") || "<li class='small'>None</li>"}</ul>

    <h3>Missing information (${d.missing_information.length})</h3>
    ${d.missing_information.length ? `<ul class="list">${d.missing_information.map((x) => `<li><b>${esc(x.field)}</b>${x.blocking ? " <span class='chip warn'>blocking</span>" : ""}<br>${esc(x.question_for_requester)}</li>`).join("")}</ul>
      <button class="btn secondary small" id="copy-q">Copy questions for requester</button>` : "<p class='small'>Nothing missing.</p>"}

    <h3>Evidence (${d.evidence.length})</h3>
    <ul class="list evidence">${d.evidence.map((e) => `<li>${esc(e.claim)}${e.value ? ` <code>${esc(e.value)}</code>` : ""}<br>
      <button class="chip link" data-call="${esc(e.call_id)}" ${toolOut(e.call_id) ? "" : "disabled"}>${esc(e.source_tool)} · ${esc(e.call_id)}</button>
      ${e.record_ids.map((r) => `<span class="chip rec-id">${esc(r)}</span>`).join("")} <span class="tag">${e.origin}</span></li>`).join("")}</ul>`;
  // Policy blocks (critical flags / reject) make Approve an exception that needs its own reason.
  const blocked = d.recommendation === "recommend_reject" || d.risk_flags.some((f) => f.severity === "critical");
  $("#exception-wrap").hidden = !blocked;
  const so = state.detail.signoffs;
  const closed = ["Approved", "Rejected"].includes(state.detail.status);
  $("#signoffs").innerHTML = so ? `Sign-offs ${so.approved.length}/${so.required.length}: ` +
    so.required.map((r) => `${esc(r)} ${so.approved.includes(r) ? "✓" : "· pending"}`).join(", ") +
    (closed ? ` — <b>closed (${esc(state.detail.status)})</b>` : "") : "";
  document.querySelectorAll("#human-form [data-action]").forEach((b) => { b.disabled = closed; });
  if (so && so.pending.length && !closed) $("#reviewer-role").value = so.pending[0];
  const copy = $("#copy-q");
  if (copy) copy.onclick = () => navigator.clipboard?.writeText(d.missing_information.map((x) => "- " + x.question_for_requester).join("\n"));
}

function showRaw(callId) {
  const ev = state.trace.find((e) => e.kind === "tool" && e.payload.call_id === callId);
  if (!ev) return;
  $("#raw-title").textContent = `${callId} · ${ev.payload.tool}`;
  $("#raw-body").textContent = JSON.stringify(ev.payload, null, 2);
  $("#raw-dialog").showModal();
}

// ---------------------------------------------------------------- workflow strip
const STEPS = ["request", "understand", "evidence", "recommend", "human"];
function setWorkflow(active) {
  const idx = active === "done" ? STEPS.length : STEPS.indexOf(active);
  document.querySelectorAll("#workflow li").forEach((li, i) => {
    li.className = i < idx ? "done" : (i === idx ? "active" : "");
  });
}

// ---------------------------------------------------------------- actions
// ---------------------------------------------------------------- live run progress
const PHASE_LABEL = {request: "Request recorded (tool result c0)", understand: "Understanding the need",
  evidence: "Gathering evidence", recommend: "Policy engine + guardrails check the draft", done: "Decision ready for human review"};
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function liveLine(ev) {
  const t = `<time>${(ev.t_ms / 1000).toFixed(1)}s</time>`;
  if (ev.kind === "phase") return `<li class="phase">${t} ▶ ${esc(PHASE_LABEL[ev.phase] || ev.phase)}${ev.recommendation ? `: <b>${esc(REC_LABEL[ev.recommendation] || ev.recommendation)}</b>` : ""}</li>`;
  if (ev.kind === "llm_start") return `<li class="muted">${t} … ${esc(ev.agent)} thinking (turn ${ev.turn})${ev.replay ? " — replayed" : ""}</li>`;
  if (ev.kind === "llm") return `<li>${t} ${ev.ok ? "✓" : "✗"} ${esc(ev.agent)} turn ${ev.turn} → ${esc((ev.tool_calls || []).join(", ") || "text")} <span class="small">${Math.round(ev.latency_ms)} ms · ${ev.tokens_in}/${ev.tokens_out} tok</span></li>`;
  if (ev.kind === "tool" && ev.tool !== "purchase_request") return `<li>${t} ${ev.ok ? "✓" : "✗"} <code>${esc(ev.call_id)}</code> ${esc(ev.tool)} <span class="small">by ${esc(ev.caller)} · ${Math.round(ev.latency_ms)} ms${ev.error ? " · " + esc(ev.error) : ""}</span></li>`;
  return "";
}

function stepFor(ev, current) {
  if (ev.kind === "phase") return ev.phase === "done" ? "human" : (ev.phase === "request" ? "request" : ev.phase);
  if (ev.kind === "tool" && ev.tool !== "purchase_request" && STEPS.indexOf(current) < STEPS.indexOf("evidence")) return "evidence";
  return current;
}

async function runCopilot() {
  const id = state.selected;
  const arch = $("#arch").value;
  const fault = $("#outage").checked ? "&fault=down" : "";
  $("#run-btn").disabled = true;
  $("#run-btn").textContent = "Running…";
  $("#live").hidden = false;
  $("#live-spinner").hidden = false;
  $("#live-title").textContent = "Running the copilot…";
  $("#live-meta").textContent = `architecture ${arch}${fault ? " · simulated vendor outage" : ""}`;
  $("#live-log").innerHTML = "";
  let step = "request";
  setWorkflow(step);
  try {
    const started = await api(`/api/requests/${encodeURIComponent(id)}/analyze?arch=${arch}${fault}&stream=true`, {method: "POST"});
    let after = 0;
    for (;;) {
      const p = await api(`/api/runs/${started.run_id}/progress?after=${after}`);
      after = p.next;
      for (const ev of p.events) {
        $("#live-log").insertAdjacentHTML("beforeend", liveLine(ev));
        step = stepFor(ev, step);
      }
      setWorkflow(step);
      $("#live-log").scrollTop = $("#live-log").scrollHeight;
      if (p.done) {
        if (p.error) throw new Error(p.error);
        break;
      }
      await sleep(400);
    }
    $("#live-title").textContent = "Run complete";
    $("#live-spinner").hidden = true;
    await loadQueue();
    await selectRequest(id);
  } catch (e) {
    $("#live-title").textContent = "Run failed";
    $("#live-spinner").hidden = true;
    $("#decision-body").hidden = false;
    $("#decision-body").innerHTML = `<div class="banner bad">Analysis failed: ${esc(e.message)}</div>`;
    setWorkflow("request");
  } finally {
    $("#run-btn").disabled = false;
    $("#run-btn").textContent = "Run copilot";
  }
}

async function humanAction(action) {
  const body = {action, reviewer_role: $("#reviewer-role").value, reason: $("#reason").value,
                exception_reason: $("#exception").value};
  try {
    await api(`/api/requests/${encodeURIComponent(state.selected)}/actions`, {method: "POST", body: JSON.stringify(body)});
    $("#form-msg").textContent = `Recorded: ${action}`;
    $("#form-msg").className = "form-msg ok-t";
    $("#reason").value = "";
    $("#exception").value = "";
    await loadQueue();
    await selectRequest(state.selected);
  } catch (e) {
    $("#form-msg").textContent = e.message;
    $("#form-msg").className = "form-msg bad-t";
  }
}

async function createRequest(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  const payload = {
    requester_id: data.requester_id, product_name: data.product_name, vendor_name: data.vendor_name,
    category: data.category || null, business_justification: data.business_justification || null,
    annual_cost_usd: data.annual_cost_usd ? Number(data.annual_cost_usd) : null,
    user_count: data.user_count ? Number(data.user_count) : null,
    data_access_level: data.data_access_level || null,
    requested_integrations: data.requested_integrations ? data.requested_integrations.split(",").map((s) => s.trim()).filter(Boolean) : [],
  };
  const created = await api("/api/requests", {method: "POST", body: JSON.stringify(payload)});
  await loadQueue();
  await selectRequest(created.request_id);
}

// ---------------------------------------------------------------- evaluation view
async function loadEval() {
  try {
    const s = await api("/api/eval/summary");
    const archs = Object.keys(s.overall || {});
    const metrics = s.headline_metrics || [];
    const PCT = /(accuracy|rate|recall|precision|exact|adherence|resistance|control|correct|pass|stability)$/;
    const fmt = (v, key = "") => {
      if (v === null || v === undefined) return "—";
      if (typeof v !== "object") return PCT.test(key) ? `${Math.round(v * 100)}%` : v;
      if (v.mean === null) return "n/a";
      const sd = v.std ? v.std : 0;
      if (key.startsWith("cost")) return "$" + v.mean.toFixed(5);
      if (PCT.test(key)) return `${(v.mean * 100).toFixed(1)}%${sd ? " ± " + (sd * 100).toFixed(1) : ""}`;
      if (key.startsWith("latency")) return `${Math.round(v.mean).toLocaleString()} ms${sd ? " ± " + Math.round(sd) : ""}`;
      return `${v.mean.toFixed(2)}${sd ? " ± " + sd.toFixed(2) : ""}`;
    };
    let html = `<h2>Evaluation: A vs B vs R</h2><p class="small">${esc(s.note || "")}</p>
      <p class="small">Full tables, every failure and the method: <code>evals/results/summary.md</code>.</p>
      <table class="eval"><thead><tr><th>Metric</th>${archs.map((a) => `<th>${esc(a)}</th>`).join("")}</tr></thead><tbody>
      ${metrics.map((m) => `<tr><td>${esc(m.label)}</td>${archs.map((a) => `<td>${esc(fmt(s.overall[a][m.key], m.key))}</td>`).join("")}</tr>`).join("")}
      </tbody></table>`;
    if (s.per_tag) {
      const tags = Object.keys(s.per_tag);
      html += `<h3>Recommendation accuracy by category</h3><table class="eval"><thead><tr><th>Category</th>${archs.map((a) => `<th>${esc(a)}</th>`).join("")}</tr></thead><tbody>
        ${tags.map((t) => `<tr><td>${esc(t)} (n=${s.per_tag[t].n})</td>${archs.map((a) => `<td>${esc(fmt(s.per_tag[t][a], "accuracy"))}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    }
    $("#eval-body").className = "";
    $("#eval-body").innerHTML = html;
  } catch (e) {
    $("#eval-body").textContent = e.message;
  }
}

// ---------------------------------------------------------------- wiring
function switchView(view) {
  $("#view-review").hidden = view !== "review";
  $("#view-eval").hidden = view !== "eval";
  $("#tab-review").classList.toggle("active", view === "review");
  $("#tab-eval").classList.toggle("active", view === "eval");
  $("#tab-review").setAttribute("aria-selected", view === "review");
  $("#tab-eval").setAttribute("aria-selected", view === "eval");
  if (view === "eval") loadEval();
}

document.addEventListener("click", (e) => {
  const item = e.target.closest(".queue-item");
  if (item) return selectRequest(item.dataset.id);
  const callBtn = e.target.closest("[data-call]");
  if (callBtn) return showRaw(callBtn.dataset.call);
  const act = e.target.closest("[data-action]");
  if (act) return humanAction(act.dataset.action);
  const sub = e.target.closest(".subtab");
  if (sub) {
    document.querySelectorAll(".subtab").forEach((b) => b.classList.toggle("active", b === sub));
    ["details", "trace", "audit"].forEach((t) => { $(`#tab-${t}`).hidden = t !== sub.dataset.tab; });
  }
});
$("#tab-review").onclick = () => switchView("review");
$("#tab-eval").onclick = () => switchView("eval");
$("#search").oninput = renderQueue;
$("#status-filter").onchange = renderQueue;
$("#run-btn").onclick = runCopilot;
$("#raw-close").onclick = () => $("#raw-dialog").close();
$("#new-btn").onclick = () => $("#new-dialog").showModal();
$("#new-form").addEventListener("submit", (e) => {
  if (e.submitter && e.submitter.value === "create") {
    e.preventDefault();
    createRequest(e.target).then(() => { $("#new-dialog").close(); e.target.reset(); })
      .catch((err) => { $("#new-msg").textContent = err.message; });
  }
});

window.addEventListener("hashchange", () => { const id = decodeURIComponent(location.hash.slice(1)); if (id) selectRequest(id); });

loadHealth();
loadQueue().then(() => {
  const id = decodeURIComponent(location.hash.slice(1));  // deep link: /#REQ-1007 opens that request
  if (id === "evaluation") switchView("eval");
  else if (id) selectRequest(id).catch(() => {});
});
setInterval(loadHealth, 15000);
