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
  $("#m-session").textContent = state.sessionId || "-";
  loadMe();
  loadInstructions();
  fetch("/api/health").then((r) => r.json()).then((h) => {
    const orch = h.checks && h.checks.orchestrator;
    if (h.status !== "ok") banner("Some ATLAS dependencies are degraded. Answers may be limited.", true);
    else if (orch && orch.status !== "ok") banner("Orchestrator unreachable: using the single-agent path.");
  }).catch(() => {});
});
