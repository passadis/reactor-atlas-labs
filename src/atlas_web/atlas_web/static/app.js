// ATLAS console. Plain JS, no build step.
//
// Scope (?scope=ep1..ep4|full) is a VIEW filter only: panels are hidden with the `hidden`
// attribute, never removed, and the server always streams every event (CLAUDE.md section 8).
"use strict";

const $ = (sel) => document.querySelector(sel);
const SCOPES = ["ep1", "ep2", "ep3", "ep4", "full"];
const state = {
  mode: "app",
  sessionId: sessionStorage.getItem("atlas.session") || null,
  busy: false,
  turnId: null,
  actAs: sessionStorage.getItem("atlas.actas") === "readonly",
};

function esc(text) {
  const d = document.createElement("div");
  d.textContent = text == null ? "" : String(text);
  return d.innerHTML;
}

// ---------------------------------------------------------------- scope (view filter) --
function scopeRank(s) { return s === "full" ? 4 : Math.max(0, SCOPES.indexOf(s)); }
function applyScope(scope) {
  const rank = scopeRank(scope);
  document.querySelectorAll("[data-scope]").forEach((el) => {
    el.hidden = scopeRank(el.dataset.scope) > rank;
  });
  const url = new URL(location.href);
  url.searchParams.set("scope", scope);
  history.replaceState(null, "", url);
  $("#scope").value = scope;
}

// --------------------------------------------------------------------------- banner --
let countdownTimer = null;
function banner(text, bad = false, seconds = null) {
  const el = $("#banner");
  clearInterval(countdownTimer);
  if (!text) { el.hidden = true; return; }
  el.className = "banner" + (bad ? " bad" : "");
  el.hidden = false;
  if (seconds) {
    let left = seconds;
    const tick = () => {
      el.textContent = `${text} Retry in ${left}s.`;
      if (left-- <= 0) { clearInterval(countdownTimer); el.hidden = true; }
    };
    tick();
    countdownTimer = setInterval(tick, 1000);
  } else {
    el.textContent = text;
  }
}

// ----------------------------------------------------------------------- identity --
function headers() {
  const h = { "Content-Type": "application/json" };
  if (state.actAs) h["x-atlas-act-as"] = "readonly";
  return h;
}
async function loadMe() {
  try {
    const r = await fetch("/api/me", { headers: headers() });
    if (!r.ok) throw new Error(r.status);
    const me = await r.json();
    const p = me.persona;
    $("#persona-name").textContent =
      `${me.name} as ${p.display_name || p.role} (${p.role})` + (me.auth === "bypass" ? " [AUTH BYPASS]" : "");
    if (me.auth === "bypass") banner("Authentication bypass is ON. This must never be the deployed app.", true);
  } catch (e) {
    $("#persona-name").textContent = "not signed in";
  }
}
async function loadInstructions() {
  try {
    const r = await fetch("/api/instructions");
    const body = await r.json();
    const sel = $("#instructions");
    body.versions.forEach((v) => {
      const o = document.createElement("option");
      o.value = v; o.textContent = v + (v === body.active ? " (active)" : "");
      sel.appendChild(o);
    });
  } catch (e) { /* the selector stays on "active" */ }
}

// -------------------------------------------------------------------- rendering --
function addMessage(cls, who, html) {
  const el = document.createElement("div");
  el.className = `msg ${cls}`;
  el.innerHTML = `<div class="who">${esc(who)}</div>${html}`;
  $(".hint")?.remove();
  $("#log").appendChild(el);
  $("#log").scrollTop = $("#log").scrollHeight;
  return el;
}
const SHAPE_LABEL = { app: "This app", model: "Model call", prompt: "Prompt agent", hosted: "Hosted agent" };

function renderTriage(el, value, caseId) {
  const cites = (value.citations || []).map((c) =>
    `<li><a href="/kb/${encodeURIComponent(c.doc_id)}" target="_blank">${esc(c.doc_id)}</a> ${esc(c.title)}` +
    (c.last_reviewed ? ` <span class="muted">(reviewed ${esc(c.last_reviewed)})</span>` : "") + `</li>`).join("");
  el.innerHTML = `
    <div class="who">ATLAS triage${caseId ? " - " + esc(caseId) : ""}</div>
    <div class="triage">
      <div class="row">
        <span class="badge ${esc(value.severity)}">${esc(value.severity)}</span>
        <span class="chip">${esc(value.category)}</span>
        <span class="chip">confidence ${Number(value.confidence).toFixed(2)}</span>
        ${value.needs_human ? '<span class="chip human">needs a human</span>' : ""}
      </div>
      <div class="prose">${esc(value.summary)}</div>
      <h4>Likely cause</h4><div class="prose">${esc(value.likely_cause)}</div>
      <h4>Affected services</h4><div>${(value.affected_services || []).map((s) => `<span class="chip">${esc(s)}</span>`).join(" ")}</div>
      <h4>Suspected changes</h4><div>${(value.suspected_change_ids || []).map((s) => `<span class="chip">${esc(s)}</span>`).join(" ") || '<span class="muted">none</span>'}</div>
      <h4>Citations</h4><ul>${cites || '<li class="muted">none</li>'}</ul>
      <pre class="contract" ${$("#contract").checked ? "" : "hidden"}>${esc(JSON.stringify(value, null, 2))}</pre>
    </div>`;
}

function toolRow(data) {
  const li = document.createElement("li");
  li.id = `tool-${data.call_id}`;
  li.innerHTML = `<span>${esc(data.name)}</span><span class="ms">...</span><span class="out running">running</span>`;
  $("#tools").appendChild(li);
}
function toolDone(data) {
  let li = document.getElementById(`tool-${data.call_id}`);
  if (!li) { toolRow(data); li = document.getElementById(`tool-${data.call_id}`); }
  li.className = data.outcome;
  li.querySelector(".ms").textContent = `${data.ms} ms`;
  const out = li.querySelector(".out");
  out.className = `out ${data.outcome}`;
  out.textContent = data.outcome === "not_found" ? "not found" : data.outcome;
  if (data.detail) li.title = data.detail;
}

// ------------------------------------------------------------------------- turns --
async function send(message) {
  if (state.busy) return;
  state.busy = true;
  $("#send").disabled = true;
  banner(null);
  addMessage("user", "You", `<div class="prose">${esc(message)}</div>`);
  $("#tools").innerHTML = "";
  ["m-first", "m-elapsed", "m-tokens", "m-trace"].forEach((id) => ($("#" + id).textContent = "-"));

  let turn;
  try {
    const r = await fetch("/api/chat", {
      method: "POST", headers: headers(),
      body: JSON.stringify({
        message, mode: state.mode, session_id: state.sessionId,
        instructions: $("#instructions").value || null,
      }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `The server refused the request (${r.status}).`);
    }
    turn = await r.json();
  } catch (e) {
    addMessage("bot err", "ATLAS", `<div class="prose">${esc(e.message)}</div>`);
    return finish();
  }
  state.sessionId = turn.session_id;
  state.turnId = turn.turn_id;
  $("#stop").hidden = false;
  sessionStorage.setItem("atlas.session", state.sessionId);
  $("#m-session").textContent = state.sessionId;
  $("#m-mode").textContent = SHAPE_LABEL[turn.mode];

  const bot = addMessage("bot", SHAPE_LABEL[turn.mode], `<div class="prose"></div>`);
  const prose = bot.querySelector(".prose");
  let finished = false;
  const es = new EventSource(`/api/chat/${turn.turn_id}/stream`);

  es.addEventListener("meta", (e) => {
    const d = JSON.parse(e.data);
    if (d.trace_id) {
      $("#m-trace").innerHTML = `<a href="/api/trace/${turn.turn_id}" target="_blank">${esc(d.trace_id)}</a>`;
    }
    if (d.instructions) $("#m-instr").textContent = `${d.instructions.version} (${d.instructions.source})`;
  });
  es.addEventListener("token", (e) => {
    prose.textContent += JSON.parse(e.data).text;
    $("#log").scrollTop = $("#log").scrollHeight;
  });
  es.addEventListener("tool_start", (e) => toolRow(JSON.parse(e.data)));
  es.addEventListener("tool_end", (e) => toolDone(JSON.parse(e.data)));
  es.addEventListener("structured", (e) => {
    const d = JSON.parse(e.data);
    if (d.kind === "incident_triage") renderTriage(bot, d.value, d.case_id);
  });
  es.addEventListener("error", (e) => {
    if (!e.data) return; // transport errors are handled by onerror below
    const d = JSON.parse(e.data);
    if (d.reason === "cancelled") {
      prose.textContent = prose.textContent || "Stopped.";
      return;
    }
    if (d.reason === "throttled") {
      banner(d.message, false, d.retry_after || 10);
    } else if (d.reason === "degraded") {
      banner(d.message);
      return;
    }
    bot.classList.add("err");
    prose.textContent = d.message;
  });
  es.addEventListener("done", (e) => {
    finished = true;
    const d = JSON.parse(e.data);
    if (d.first_event_ms != null) $("#m-first").textContent = `${d.first_event_ms} ms`;
    if (d.elapsed_ms != null) $("#m-elapsed").textContent = `${(d.elapsed_ms / 1000).toFixed(1)} s`;
    if (d.input_tokens) $("#m-tokens").textContent = d.input_tokens.toLocaleString();
    if (d.instructions_version) $("#m-instr").textContent = d.instructions_version;
    // Foundry agent citation markers (U+3010 ... U+3011) are for machines;
    // the activity panel already shows the search calls.
    prose.textContent = prose.textContent.replace(/\u3010[^\u3011]*\u3011/g, "");
    if (!prose.textContent && !bot.querySelector(".triage")) prose.textContent = "(no answer)";
    es.close();
    finish();
  });
  es.onerror = () => {
    if (finished) return;
    es.close();
    bot.classList.add("err");
    if (!prose.textContent) prose.textContent = "The connection to ATLAS dropped. The run was stopped; try again.";
    finish();
  };
}
function finish() {
  state.busy = false;
  state.turnId = null;
  $("#stop").hidden = true;
  $("#send").disabled = false;
  $("#message").focus();
}

// -------------------------------------------------------------------------- wiring --
document.addEventListener("DOMContentLoaded", () => {
  const params = new URLSearchParams(location.search);
  applyScope(SCOPES.includes(params.get("scope")) ? params.get("scope") : "ep1");
  $("#scope").addEventListener("change", (e) => applyScope(e.target.value));

  $("#modes").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-mode]");
    if (!b) return;
    state.mode = b.dataset.mode;
    document.querySelectorAll("#modes button").forEach((x) => x.classList.toggle("on", x === b));
    $("#m-mode").textContent = SHAPE_LABEL[state.mode];
  });
  $("#contract").addEventListener("change", (e) =>
    document.querySelectorAll(".contract").forEach((p) => (p.hidden = !e.target.checked)));
  $("#actas").checked = state.actAs;
  $("#actas").addEventListener("change", (e) => {
    state.actAs = e.target.checked;
    sessionStorage.setItem("atlas.actas", state.actAs ? "readonly" : "");
    loadMe();
  });
  // Container Apps ingress does not tell the server that a streaming client went away, so
  // stopping is explicit: the Stop button, and a beacon when the tab closes or navigates.
  const cancelTurn = (beacon) => {
    if (!state.turnId) return;
    const url = `/api/chat/${state.turnId}/cancel`;
    if (beacon && navigator.sendBeacon) navigator.sendBeacon(url);
    else fetch(url, { method: "POST", headers: headers(), keepalive: true });
  };
  $("#stop").addEventListener("click", () => cancelTurn(false));
  window.addEventListener("pagehide", () => cancelTurn(true));
  $("#new-session").addEventListener("click", () => {
    cancelTurn(false);
    state.sessionId = null;
    sessionStorage.removeItem("atlas.session");
    $("#log").innerHTML = "";
    $("#tools").innerHTML = "";
    $("#m-session").textContent = "-";
  });
  document.querySelectorAll(".example").forEach((b) =>
    b.addEventListener("click", () => { $("#message").value = b.textContent.trim(); $("#message").focus(); }));
  $("#composer").addEventListener("submit", (e) => {
    e.preventDefault();
    const text = $("#message").value.trim();
    if (!text) return;
    $("#message").value = "";
    send(text);
  });
  $("#message").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#composer").requestSubmit(); }
  });
  $("#orch-mode").addEventListener("change", loadGraph);
  $("#wf-approve").addEventListener("click", () => decide("approve"));
  $("#wf-reject").addEventListener("click", () => decide("reject"));
  $("#run-workflow").addEventListener("click", () => {
    const text = $("#message").value.trim() || document.querySelector(".example").textContent.trim();
    $("#message").value = "";
    runWorkflow(text);
  });
  window.addEventListener("pagehide", () => { if (wf.es) wf.es.close(); });

  $("#m-session").textContent = state.sessionId || "-";
  loadMe();
  loadInstructions();
  loadGraph();
  fetch("/api/health").then((r) => r.json()).then((h) => {
    const orch = h.checks && h.checks.orchestrator;
    if (h.status !== "ok") banner("Some ATLAS dependencies are degraded. Answers may be limited.", true);
    else if (orch && orch.status !== "ok") banner("Orchestrator unreachable: the chat path still works, the workflow does not.");
  }).catch(() => {});
});

// ------------------------------------------------------- workflow (Episode 2) --
//
// The graph is drawn from the orchestrator's own topology, not from a hand-drawn picture: if the
// shape changes, this panel changes with it. Nodes are laid out in run order, with the concurrent
// pair marked so the overlap is visible while it happens.
const NODE_LABEL = {
  intake: "intake", triage_agent: "triage", knowledge_agent: "knowledge",
  impact_agent: "impact", aggregate: "aggregate", planner_agent: "planner", report: "report",
  coordinator_agent: "coordinator",
};
const wf = { caseId: null, es: null, nodes: new Map(), started: 0, busy: false,
             approval: null };

function wfOrder(topology) {
  // Walk the edges from the start node so the panel reflects the deployed graph, not this file.
  const out = new Map();
  (topology.edges || []).forEach((e) => {
    if (!out.has(e.from)) out.set(e.from, []);
    out.get(e.from).push(e.to);
  });
  // Breadth-first: a depth-first descent follows one branch of the fan-out all the way to
  // `report` before coming back for the other, which puts the two parallel nodes far apart.
  const start = (topology.nodes || []).find((n) => n.start);
  const order = [];
  const seen = new Set();
  const queue = [start && start.id];
  while (queue.length) {
    const id = queue.shift();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    order.push(id);
    (out.get(id) || []).forEach((next) => queue.push(next));
  }
  (topology.nodes || []).forEach((n) => { if (!seen.has(n.id)) { seen.add(n.id); order.push(n.id); } });
  return order;
}

async function loadGraph() {
  const mode = $("#orch-mode").value;
  try {
    const r = await fetch(`/api/workflow/graph?mode=${encodeURIComponent(mode)}`, { headers: headers() });
    if (!r.ok) throw new Error(r.status);
    const topology = await r.json();
    $("#wf-mermaid-src").textContent = topology.mermaid || "-";
    const fanIn = new Set((topology.edges || []).filter((e) => e.kind === "fanin").map((e) => e.from));
    $("#wf-nodes").innerHTML = wfOrder(topology).map((id) => {
      const node = (topology.nodes || []).find((n) => n.id === id) || {};
      return `<li class="wf-node" data-node="${esc(id)}" data-kind="${esc(node.kind || "")}">
        <span class="wf-dot"></span>
        <span class="wf-name">${esc(NODE_LABEL[id] || id)}</span>
        ${fanIn.has(id) ? '<span class="wf-tag" title="Runs concurrently with its sibling">parallel</span>' : ""}
        ${node.kind === "deterministic" ? '<span class="wf-tag code">code</span>' : '<span class="wf-tag agent">agent</span>'}
        <span class="wf-time"></span>
      </li>`;
    }).join("");
  } catch (e) {
    $("#wf-nodes").innerHTML = `<li class="wf-node err">Orchestrator unreachable.</li>`;
  }
}

function wfNode(id) { return document.querySelector(`.wf-node[data-node="${CSS.escape(id)}"]`); }
function wfMark(id, cls, text) {
  const el = wfNode(id);
  if (!el) return;
  el.classList.remove("running", "done", "degraded");
  el.classList.add(cls);
  if (text != null) el.querySelector(".wf-time").textContent = text;
}
function wfReset() {
  document.querySelectorAll(".wf-node").forEach((el) => {
    el.classList.remove("running", "done", "degraded");
    el.querySelector(".wf-time").textContent = "";
  });
  $("#wf-decisions").innerHTML = "";
  $("#wf-approval").hidden = true;
  wf.approval = null;
  ["#wf-case", "#wf-sev", "#wf-plan", "#wf-elapsed"].forEach((s) => ($(s).textContent = "-"));
}

async function runWorkflow(message) {
  if (wf.busy) return;
  wf.busy = true;
  wfReset();
  $("#run-workflow").disabled = true;
  const mode = $("#orch-mode").value;
  let start;
  try {
    const r = await fetch("/api/workflow", {
      method: "POST", headers: headers(), body: JSON.stringify({ message, mode }),
    });
    if (!r.ok) {
      const body = await r.json().catch(() => ({}));
      throw new Error(body.detail || `The orchestrator refused the case (${r.status}).`);
    }
    start = await r.json();
  } catch (e) {
    banner(e.message, true);
    wf.busy = false;
    $("#run-workflow").disabled = false;
    return;
  }
  wf.caseId = start.case_id;
  wf.started = performance.now();
  $("#wf-case").textContent = start.case_id;
  const bot = addMessage("bot", `Workflow (${mode})`, `<div class="prose">Running the case through the workflow...</div>`);
  const prose = bot.querySelector(".prose");

  const es = new EventSource(`/api/workflow/${start.case_id}/stream`);
  wf.es = es;
  const done = () => {
    es.close();
    wf.busy = false;
    wf.es = null;
    $("#run-workflow").disabled = false;
    $("#wf-elapsed").textContent = `${((performance.now() - wf.started) / 1000).toFixed(1)} s`;
  };
  es.addEventListener("executor_invoked", (e) => {
    const d = JSON.parse(e.data);
    if (d.node && wfNode(d.node)) wfMark(d.node, "running", "...");
  });
  es.addEventListener("node", (e) => {
    const d = JSON.parse(e.data);
    const p = d.data || {};
    const tools = (p.tool_calls || []).length;
    wfMark(d.node, p.ok ? "done" : "degraded",
      `${(p.seconds ?? 0).toFixed ? p.seconds.toFixed(1) : p.seconds}s` + (tools ? ` / ${tools} tools` : ""));
  });
  es.addEventListener("approval_required", (e) => {
    showApproval(JSON.parse(e.data).data || {});
    prose.innerHTML = `<p>Waiting on a human. The case is checkpointed - it survives a restart.</p>`;
  });
  es.addEventListener("resuming", (e) => {
    const d = JSON.parse(e.data).data || {};
    hideApproval(`Resuming: ${esc(d.approved ? "approved" : "rejected")} by ${esc(d.by || "")}.`);
  });
  es.addEventListener("awaiting_approval", () => {
    hideApproval("Still waiting on a human. The case is checkpointed and resumable at any time.");
  });
  es.addEventListener("report", (e) => {
    const r = JSON.parse(e.data).data || {};
    $("#wf-sev").textContent = r.severity || "-";
    $("#wf-plan").textContent = r.plan_id ? `${r.plan_id} (${r.plan_steps} steps)` : "-";
    wfMark("report", "done", "");
    (r.decisions || []).forEach((d) => {
      const li = document.createElement("li");
      li.innerHTML = `<b>${esc(d.by)}</b> <code>${esc(d.rule)}</code> ${esc(d.detail)}`;
      $("#wf-decisions").append(li);
    });
    const missing = (r.missing_analysis || []).length
      ? `<p class="muted">Missing analysis: ${esc((r.missing_analysis || []).join(", "))}.</p>` : "";
    prose.innerHTML = `<p><b>${esc(r.severity)}</b> - ${esc(r.headline || "")}</p>
      <p>${esc(r.summary || "")}</p>${missing}
      <p class="muted">${r.needs_human ? "Needs a human before anything runs." : "No human review required by policy."}</p>`;
  });
  es.addEventListener("restored", (e) => {
    prose.textContent = "This case already ran. Showing its stored result.";
    const d = (JSON.parse(e.data).data || {}).result || {};
    if (d.severity) $("#wf-sev").textContent = d.severity;
    done();
  });
  es.addEventListener("error", (e) => {
    if (!e.data) return;
    const d = JSON.parse(e.data);
    bot.classList.add("err");
    prose.textContent = (d.data && d.data.message) || d.message || "The workflow failed.";
  });
  es.addEventListener("end", done);
  es.onerror = () => {
    if (!wf.busy) return;
    bot.classList.add("err");
    if (!prose.textContent) prose.textContent = "The connection to the workflow dropped.";
    done();
  };
}

// ------------------------------------------------- approval gate (Episode 2, second half) --
//
// The panel prints the policy gate's `reasons` verbatim. They are written by code, not by an
// agent, so an approver is never reading the model's account of why it should be allowed to
// proceed. Who is approving comes from the signed-in token, never from this page.
function showApproval(request) {
  wf.approval = request;
  $("#wf-approval-roles").textContent =
    request.required_roles && request.required_roles.length
      ? `(${request.required_roles.join(" or ")})` : "";
  $("#wf-approval-reasons").innerHTML = (request.reasons || [])
    .map((r) => `<li>${esc(r)}</li>`).join("");
  const expires = request.expires_at
    ? `Expires ${new Date(request.expires_at).toLocaleTimeString()}. ` : "";
  $("#wf-approval-plan").textContent =
    `${expires}Plan ${request.plan_id} - ${request.destructive_steps} destructive step(s) on `
    + `${(request.targets || []).join(", ")}, blast radius ${request.blast_radius}.\n`
    + (request.plan_summary || []).join("\n");
  $("#wf-approval-note").textContent = "";
  $("#wf-approval").hidden = false;
  $("#wf-comment").focus();
}

function hideApproval(note) {
  $("#wf-approval").hidden = true;
  if (note) $("#wf-approval-note").textContent = note;
}

async function decide(decision) {
  if (!wf.approval) return;
  const comment = $("#wf-comment").value.trim();
  if (comment.length < 3) {
    $("#wf-approval-note").textContent = "A comment is required: a decision without a reason is not an audit trail.";
    return;
  }
  const caseId = wf.approval.case_id;
  $("#wf-approve").disabled = true;
  $("#wf-reject").disabled = true;
  try {
    const r = await fetch(`/api/workflow/${caseId}/decision`, {
      method: "POST", headers: headers(), body: JSON.stringify({ decision, comment }),
    });
    const body = await r.json().catch(() => ({}));
    if (!r.ok) {
      // A refusal is part of the demo, not an error: the signed-in persona may not be allowed to
      // approve this particular plan, and the case stays parked.
      $("#wf-approval-note").textContent = body.detail || `Refused (${r.status}).`;
      return;
    }
    $("#wf-comment").value = "";
    hideApproval(`${decision === "approve" ? "Approved" : "Rejected"}; the case is resuming from its checkpoint.`);
  } catch (e) {
    $("#wf-approval-note").textContent = "Could not reach ATLAS. The case is checkpointed and still resumable.";
  } finally {
    $("#wf-approve").disabled = false;
    $("#wf-reject").disabled = false;
  }
}
