/* FR Copilot SPA: Cognito sign-in, client picker, streaming chat, MCP tool-call panel. No dependencies. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { llm: null, w: null, dragBase: null, llmTimer: null, signup: false, cfg: null, tokens: null, clients: [], client: null, sessionId: null, busy: false, toolCount: 0, turn: 0 };
  const store = {
    get(k) { try { return JSON.parse(sessionStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
    del(k) { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
  };
  const fmt$ = (n) => (typeof n === "number" ? n.toLocaleString("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 }) : "-");
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // ---------- Cognito (USER_PASSWORD_AUTH over the public JSON API) ----------
  async function cognito(target, body) {
    const r = await fetch(`https://cognito-idp.${state.cfg.region}.amazonaws.com/`, {
      method: "POST",
      headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}` },
      body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error(j.message || j.__type || "Sign-in failed");
    return j;
  }
  function claims(jwt) {
    try { return JSON.parse(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); } catch { return {}; }
  }
  async function signUp(username, password) {
    // Self sign-up: the PreSignUp Lambda auto-confirms the ID, so no email or code is involved.
    await cognito("SignUp", { ClientId: state.cfg.clientId, Username: username, Password: password });
  }
  function setMode(signup) {
    state.signup = signup;
    $("mode-signin").classList.toggle("active", !signup); $("mode-signin").setAttribute("aria-selected", String(!signup));
    $("mode-signup").classList.toggle("active", signup); $("mode-signup").setAttribute("aria-selected", String(signup));
    $("confirm-wrap").hidden = !signup; $("signup-help").hidden = !signup; $("password2").required = signup;
    $("password").autocomplete = signup ? "new-password" : "current-password";
    $("login-btn").textContent = signup ? "Create account and sign in" : "Sign in";
    $("login-error").hidden = true;
  }
  async function login(username, password) {
    const j = await cognito("InitiateAuth", { AuthFlow: "USER_PASSWORD_AUTH", ClientId: state.cfg.clientId, AuthParameters: { USERNAME: username, PASSWORD: password } });
    if (!j.AuthenticationResult) throw new Error("Additional sign-in challenge required; contact the instructor.");
    setTokens(j.AuthenticationResult);
  }
  function setTokens(ar) {
    const prev = state.tokens || {};
    state.tokens = { access: ar.AccessToken, refresh: ar.RefreshToken || prev.refresh, exp: claims(ar.AccessToken).exp || 0 };
    store.set("frc.tokens", state.tokens);
  }
  async function accessToken() {
    if (!state.tokens) throw new Error("Not signed in");
    if (Date.now() / 1000 > state.tokens.exp - 120) {
      const j = await cognito("InitiateAuth", { AuthFlow: "REFRESH_TOKEN_AUTH", ClientId: state.cfg.clientId, AuthParameters: { REFRESH_TOKEN: state.tokens.refresh } });
      setTokens(j.AuthenticationResult);
    }
    return state.tokens.access;
  }
  async function logout() {
    try { if (state.tokens?.refresh) await cognito("RevokeToken", { ClientId: state.cfg.clientId, Token: state.tokens.refresh }); } catch { /* best effort */ }
    state.tokens = null; store.del("frc.tokens"); location.reload();
  }

  // ---------- Views ----------
  function showLogin(msg) {
    $("app-view").hidden = true; $("login-view").hidden = false;
    if (msg) { $("login-error").textContent = msg; $("login-error").hidden = false; }
    $("username").focus();
  }
  async function showApp() {
    $("login-view").hidden = true; $("app-view").hidden = false;
    $("whoami").textContent = claims(state.tokens.access).username || "";
    loadLlm();
    setInterval(() => { if ($("llm-panel").hidden) loadLlm(); }, 60000);
    await loadClients();
  }

  // ---------- Clients ----------
  async function api(path, opts = {}) {
    const r = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), Authorization: `Bearer ${await accessToken()}` } });
    if (r.status === 401) { showLogin("Your session expired. Please sign in again."); throw new Error("unauthorized"); }
    return r;
  }
  async function loadClients() {
    const r = await api("/api/clients");
    state.clients = (await r.json()).clients || [];
    renderClients();
    const last = store.get("frc.client");
    const pick = state.clients.find((c) => c.client_id === last) || state.clients.find((c) => c.client_id === "C-007") || state.clients[0];
    if (pick) selectClient(pick.client_id);
  }
  function renderClients() {
    const q = $("client-search").value.trim().toLowerCase();
    const rows = state.clients.filter((c) => !q || `${c.client_id} ${c.first_name} ${c.last_name} ${c.occupation}`.toLowerCase().includes(q));
    $("client-count").textContent = `${rows.length} synthetic`;
    $("client-list").innerHTML = rows.map((c) => `
      <li class="client-item" role="option" tabindex="0" data-id="${esc(c.client_id)}" aria-selected="${state.client?.client_id === c.client_id}">
        <span class="name">${esc(c.first_name)} ${esc(c.last_name)}</span><span class="id">${esc(c.client_id)}</span>
        <span class="meta">${esc(c.age)} · ${esc(c.occupation)} · ${esc(c.state)}</span>
      </li>`).join("");
  }
  function selectClient(id) {
    const c = state.clients.find((x) => x.client_id === id);
    if (!c || state.busy) return;
    const changed = state.client?.client_id !== id;
    state.client = c; store.set("frc.client", id);
    renderClients();
    const card = $("client-card");
    card.hidden = false;
    card.innerHTML = `<span class="cname">${esc(c.first_name)} ${esc(c.last_name)} <span class="muted small">${esc(c.client_id)}</span></span>
      <span class="kv">Age <b>${esc(c.age)}</b></span><span class="kv">Income <b>${fmt$(c.annual_income)}</b></span>
      <span class="kv">${esc(c.marital_status)} · <b>${esc(c.dependents)}</b> dependent${c.dependents === 1 ? "" : "s"}</span>
      <span class="kv">Target retirement <b>${esc(c.target_retirement_age)}</b></span><span class="kv">Rep <b>${esc(c.assigned_rep)}</b></span>`;
    if (changed) newChat();
  }

  // ---------- Chat ----------
  function newChat() {
    state.sessionId = `${crypto.randomUUID()}-${(state.client?.client_id || "none").replace(/[^A-Za-z0-9-]/g, "")}`;
    state.toolCount = 0; state.turn = 0;
    $("tool-log").innerHTML = ""; $("tool-empty").hidden = false; updateToolSummary();
    const c = state.client;
    const name = c ? c.first_name : "this client";
    const sugg = c ? [
      `Is client ${c.client_id} on track to retire at 65, and do they have a coverage gap?`,
      `How much more would ${name} need to save each month to retire at ${Math.max(c.age + 1, 62)}?`,
      `Summarize ${name}'s in-force policies and when any term coverage expires.`,
      "Remember for future chats: I prefer the income-replacement method and brief answers.",
    ] : [];
    $("messages").innerHTML = `<div class="welcome"><h3>Ask about ${esc(c ? `${c.first_name} ${c.last_name}` : "a client")}</h3>
      <div>The agent pulls client data from <b>client-profile-mcp</b> and runs the numbers through <b>planning-calc-mcp</b>, both behind one AgentCore Gateway.</div>
      <div class="suggestions">${sugg.map((s) => `<button class="suggestion" type="button">${esc(s)}</button>`).join("")}</div></div>`;
    updateSend();
  }
  function addMsg(cls, html) {
    const w = $("messages").querySelector(".welcome"); if (w) w.remove();
    const el = document.createElement("div"); el.className = `msg ${cls}`; el.innerHTML = html;
    $("messages").appendChild(el); scrollDown(); return el;
  }
  function scrollDown() { const m = $("messages"); m.scrollTop = m.scrollHeight; }
  function updateSend() { $("send").disabled = state.busy || !$("prompt").value.trim() || !state.client; }

  async function send(text) {
    if (state.busy || !text.trim() || !state.client) return;
    state.busy = true; updateSend(); state.turn += 1;
    addMsg("user", esc(text));
    const bot = addMsg("bot", `<div class="md"><span class="typing"><i></i><i></i><i></i></span></div>`);
    const body = bot.querySelector(".md");
    let md = ""; let done = null; let guard = false; let failed = null; let modelUsed = null;
    toolTurnSeparator(text);
    try {
      const r = await api("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" },
        body: JSON.stringify({ prompt: text, client_id: state.client.client_id, session_id: state.sessionId }),
      });
      const reader = r.body.getReader(); const dec = new TextDecoder(); let buf = "";
      for (;;) {
        const { value, done: end } = await reader.read();
        if (end) break;
        buf += dec.decode(value, { stream: true });
        let i;
        while ((i = buf.indexOf("\n\n")) >= 0) {
          const frame = buf.slice(0, i); buf = buf.slice(i + 2);
          for (const line of frame.split("\n")) {
            if (!line.startsWith("data:")) continue;
            let ev; try { ev = JSON.parse(line.slice(5).trim()); } catch { continue; }
            if (typeof ev === "string") ev = { type: "text", data: ev };
            if (ev.type === "text") { md += ev.data; body.innerHTML = renderMd(md); scrollDown(); }
            else if (ev.type === "model") { modelUsed = ev; setChip(ev.name); }
            else if (ev.type === "tool_start") toolStart(ev);
            else if (ev.type === "tool_end") toolEnd(ev);
            else if (ev.type === "guardrail") guard = true;
            else if (ev.type === "error") failed = ev.message;
            else if (ev.type === "done") done = ev;
          }
        }
      }
    } catch (e) {
      if (e.message !== "unauthorized") failed = "Network error. Please retry.";
    }
    if (failed && !md) { bot.classList.add("error"); body.textContent = failed; }
    else if (failed) body.insertAdjacentHTML("beforeend", `<p class="error">${esc(failed)}</p>`);
    if (guard) bot.insertAdjacentHTML("beforeend", `<div class="guard">🛡️ Amazon Bedrock Guardrail intervened on this response.</div>`);
    if (done) bot.insertAdjacentHTML("beforeend", `<div class="meta">${esc(done.model_name || modelUsed?.name || "")}${modelUsed?.vendor ? ` (${esc(modelUsed.vendor)})` : ""} · ${done.tool_calls} tool call${done.tool_calls === 1 ? "" : "s"} · ${(done.ms / 1000).toFixed(1)} s</div>`);
    state.busy = false; updateSend(); $("prompt").focus();
  }

  // ---------- LLM routing (AppConfig via /api/llm) ----------
  const KEYS = ["quality", "cost", "latency"];
  const usd = (n) => (n >= 0.01 ? `$${n.toFixed(3)}` : `$${n.toFixed(4)}`);
  function setChip(name) { if (name) $("model-name").textContent = name; }
  async function loadLlm() {
    try {
      const r = await api("/api/llm");
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "unavailable");
      state.llm = j; setChip(j.active_model_name);
      const draftDirty = state.w && KEYS.some((k) => state.w[k] !== j.weights[k]);
      if (!draftDirty || $("llm-panel").hidden) state.w = { ...j.weights };
      if (!$("llm-panel").hidden) renderLlm();
    } catch (e) { if (e.message !== "unauthorized") $("llm-status").textContent = `Could not load routing settings: ${e.message}`; }
  }
  function openLlm() {
    $("llm-panel").hidden = false; $("llm-backdrop").hidden = false;
    if (state.llm) { state.w = { ...state.llm.weights }; renderLlm(); }
    loadLlm();
    clearInterval(state.llmTimer); state.llmTimer = setInterval(loadLlm, 20000);
    $("w-quality").focus();
  }
  function closeLlm() { $("llm-panel").hidden = true; $("llm-backdrop").hidden = true; clearInterval(state.llmTimer); $("model-btn").focus(); }
  function rebalance(key, v) {
    // Keep the total at exactly 100: split the remainder over the other two in proportion to where they were when the drag began.
    const base = state.dragBase || state.w;
    const [a, b] = KEYS.filter((k) => k !== key);
    const rest = 100 - v; const sum = base[a] + base[b];
    const wa = sum === 0 ? Math.floor(rest / 2) : Math.round((rest * base[a]) / sum);
    state.w = { ...state.w, [key]: v, [a]: wa, [b]: rest - wa };
  }
  function draftRanking() {
    const w = state.w; const rows = (state.llm?.ranking || []).map((r) => ({ ...r, draft: (w.quality * r.quality_score + w.cost * r.cost_score + w.latency * r.latency_score) / 100 }));
    return rows.sort((x, y) => y.draft - x.draft || x.cost_per_question_usd - y.cost_per_question_usd);
  }
  function renderLlm() {
    const j = state.llm; if (!j || !state.w) return;
    for (const k of KEYS) { $(`w-${k}`).value = state.w[k]; $(`o-${k}`).textContent = `${state.w[k]}%`; }
    $("mix-q").style.width = `${state.w.quality}%`; $("mix-c").style.width = `${state.w.cost}%`; $("mix-l").style.width = `${state.w.latency}%`;
    const dirty = KEYS.some((k) => state.w[k] !== j.weights[k]);
    const rows = dirty ? draftRanking() : j.ranking;
    const cw = j.weights;
    $("rank-basis").textContent = dirty ? `your draft Q${state.w.quality}/C${state.w.cost}/L${state.w.latency} (preview, not applied)` : `current settings Q${cw.quality}/C${cw.cost}/L${cw.latency}`;
    $("llm-panel").classList.toggle("dirty", dirty);
    $("elig-min").textContent = j.eligibility_min_task_score;
    const bestEligible = rows.find((r) => r.eligible);
    $("llm-preview").innerHTML = dirty && bestEligible
      ? (bestEligible.id === j.active_model_id ? `With these weights, <b>${esc(bestEligible.name)}</b> (already live) stays the best eligible model.` : `With these weights, the best eligible model would be <b>${esc(bestEligible.name)}</b>. Click <b>Evaluate &amp; apply</b> to switch the live app.`)
      : "";
    $("rank-body").innerHTML = rows.map((r, i) => {
      const score = dirty ? r.draft : r.score;
      const tags = `${r.active ? '<span class="pill live">live</span>' : ""}${r.eligible ? "" : `<span class="pill inelig" title="Scored ${r.task_score}/100 on the FR Copilot task (minimum ${j.eligibility_min_task_score})">not eligible</span>`}`;
      return `<tr class="${r.active ? "is-active" : ""} ${r.eligible ? "" : "is-inelig"}">
        <td class="rk">${i + 1}</td>
        <td><div class="mname">${esc(r.name)} ${tags}</div><div class="muted tiny">${esc(r.vendor)} · ${esc(r.strengths)}</div></td>
        <td class="r"><div class="scorebar"><i style="width:${Math.max(2, score).toFixed(0)}%"></i></div><b>${score.toFixed(1)}</b></td>
        <td class="r" title="Task ${r.task_score}/100, general ${r.general_quality}">${r.task_score}<span class="muted tiny">/100</span></td>
        <td class="r" title="$${r.price_in}/$${r.price_out} per 1M in/out tokens">${usd(r.cost_per_question_usd)}</td>
        <td class="r">${r.latency_s.toFixed(1)} s</td></tr>`;
    }).join("");
    const lc = j.last_change; const dep = j.deployment;
    $("llm-status").textContent = `AppConfig v${j.config_version}${dep ? ` · deployment #${dep.number} ${dep.state.toLowerCase()}` : ""}${lc ? ` · last change by ${lc.by}, ${new Date(lc.at).toLocaleTimeString()}` : ""}`;
  }
  async function applyLlm() {
    const btn = $("llm-apply"); btn.disabled = true; btn.textContent = "Evaluating…";
    const box = $("llm-decision");
    try {
      const r = await api("/api/llm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(state.w) });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || `HTTP ${r.status}`);
      state.llm = j; state.w = { ...j.weights }; setChip(j.active_model_name);
      const d = j.decision;
      box.className = `decision ${d.switch ? "switched" : "kept"}`; box.hidden = false;
      box.innerHTML = `<b>${d.switch ? `Switched the live app to ${esc(j.active_model_name)}` : `Kept ${esc(j.active_model_name)}`}</b><br>${esc(d.reason)}<br><span class="muted tiny">Saved as AppConfig version ${j.config_version} and deployed. New chat turns use it within about 15 seconds.</span>`;
      renderLlm();
    } catch (e) {
      if (e.message !== "unauthorized") { box.className = "decision error"; box.hidden = false; box.textContent = `Could not apply: ${e.message}`; }
    } finally { btn.disabled = false; btn.textContent = "Evaluate & apply"; }
  }

  // ---------- Tool panel ----------
  const chipClass = (s) => (s === "client-profile-mcp" ? "profile" : s === "planning-calc-mcp" ? "calc" : "other");
  function toolTurnSeparator(text) {
    const li = document.createElement("li"); li.className = "turn-sep";
    li.textContent = `Turn ${state.turn}: ${text.length > 48 ? `${text.slice(0, 48)}…` : text}`;
    $("tool-log").appendChild(li);
  }
  function toolStart(ev) {
    state.toolCount += 1; $("tool-empty").hidden = true;
    const li = document.createElement("li"); li.className = "tool"; li.id = `tool-${ev.id}`;
    li.innerHTML = `<div class="row"><span class="chip ${chipClass(ev.server)}">${esc(ev.server)}</span><span class="status run">running</span></div>
      <div class="tname">${esc(ev.tool)}</div>
      <details><summary>arguments</summary><pre>${esc(JSON.stringify(ev.input || {}, null, 2))}</pre></details>`;
    $("tool-log").appendChild(li); li.scrollIntoView({ block: "nearest" });
    updateToolSummary();
  }
  function toolEnd(ev) {
    const st = document.querySelector(`#tool-${CSS.escape(ev.id)} .status`);
    if (!st) return;
    st.className = `status ${ev.status === "success" ? "ok" : "err"}`;
    st.textContent = `${ev.ms} ms`;
  }
  function updateToolSummary() {
    const items = [...document.querySelectorAll("#tool-log .tool .chip")].map((c) => c.textContent);
    const by = items.reduce((a, s) => ((a[s] = (a[s] || 0) + 1), a), {});
    $("tool-summary").textContent = Object.entries(by).map(([k, v]) => `${k.replace("-mcp", "")} ×${v}`).join(" · ");
    $("tool-badge").textContent = String(state.toolCount);
  }

  // ---------- Tiny safe markdown (escape first, then format) ----------
  function inline(s) {
    return s.replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
  }
  function renderMd(src) {
    const lines = esc(src).split("\n"); const out = []; let i = 0;
    const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
    const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
    while (i < lines.length) {
      const l = lines[i];
      if (isRow(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const head = cells(l); i += 2; const body = [];
        while (i < lines.length && isRow(lines[i])) body.push(cells(lines[i++]));
        out.push(`<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("")}</tbody></table>`);
        continue;
      }
      let m;
      if ((m = l.match(/^(#{1,4})\s+(.*)$/))) { out.push(`<h3>${inline(m[2])}</h3>`); i++; continue; }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
      if (/^\s*[-*•]\s+/.test(l)) {
        const items = []; while (i < lines.length && /^\s*[-*•]\s+/.test(lines[i])) items.push(inline(lines[i++].replace(/^\s*[-*•]\s+/, "")));
        out.push(`<ul>${items.map((x) => `<li>${x}</li>`).join("")}</ul>`); continue;
      }
      if (/^\s*\d+[.)]\s+/.test(l)) {
        const items = []; while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(inline(lines[i++].replace(/^\s*\d+[.)]\s+/, "")));
        out.push(`<ol>${items.map((x) => `<li>${x}</li>`).join("")}</ol>`); continue;
      }
      if (!l.trim()) { i++; continue; }
      const para = []; while (i < lines.length && lines[i].trim() && !isRow(lines[i]) && !/^(#{1,4})\s|^\s*[-*•]\s|^\s*\d+[.)]\s|^\s*-{3,}\s*$/.test(lines[i])) para.push(inline(lines[i++]));
      if (para.length) out.push(`<p>${para.join("<br>")}</p>`); else i++;
    }
    return out.join("");
  }

  // ---------- Wiring ----------
  async function init() {
    try { state.cfg = await (await fetch("config.json", { cache: "no-store" })).json(); } catch { document.body.textContent = "Configuration unavailable."; return; }
    $("login-form").addEventListener("submit", async (e) => {
      e.preventDefault(); $("login-error").hidden = true; $("login-btn").disabled = true;
      const u = $("username").value.trim(); const pw = $("password").value;
      try {
        if (state.signup) {
          if (!/^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$/.test(u)) throw new Error("Username must be 3-32 letters, digits, '.', '_' or '-'.");
          if (pw.length < 6) throw new Error("Password must be at least 6 characters.");
          if (pw !== $("password2").value) throw new Error("Passwords do not match.");
          await signUp(u, pw);
        }
        await login(u, pw); $("password").value = ""; $("password2").value = ""; await showApp();
      } catch (err) {
        const m = err.message || "failed";
        $("login-error").textContent = m === "Incorrect username or password." || state.signup ? m.replace(/^PreSignUp failed with error /, "") : `Sign-in failed: ${m}`;
        $("login-error").hidden = false;
      } finally { $("login-btn").disabled = false; }
    });
    $("mode-signin").addEventListener("click", () => setMode(false));
    $("mode-signup").addEventListener("click", () => setMode(true));
    if (state.cfg.insightsUrl) document.querySelectorAll(".insights-link").forEach((a) => { a.href = state.cfg.insightsUrl; });
    $("logout").addEventListener("click", logout);
    $("model-btn").addEventListener("click", openLlm);
    $("llm-close").addEventListener("click", closeLlm);
    $("llm-backdrop").addEventListener("click", closeLlm);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("llm-panel").hidden) closeLlm(); });
    for (const k of KEYS) {
      const el = $(`w-${k}`);
      el.addEventListener("input", () => { if (!state.dragBase) state.dragBase = { ...state.w }; rebalance(k, Number(el.value)); renderLlm(); });
      el.addEventListener("change", () => { state.dragBase = null; });
    }
    $("llm-reset").addEventListener("click", () => { state.w = { quality: 50, cost: 25, latency: 25 }; renderLlm(); });
    $("llm-apply").addEventListener("click", applyLlm);
    $("new-chat").addEventListener("click", () => { if (!state.busy) newChat(); });
    $("toggle-tools").addEventListener("click", () => $("tools-pane").classList.toggle("open"));
    $("client-search").addEventListener("input", renderClients);
    $("client-list").addEventListener("click", (e) => { const li = e.target.closest(".client-item"); if (li) selectClient(li.dataset.id); });
    $("client-list").addEventListener("keydown", (e) => { const li = e.target.closest(".client-item"); if (li && (e.key === "Enter" || e.key === " ")) { e.preventDefault(); selectClient(li.dataset.id); } });
    $("messages").addEventListener("click", (e) => { const b = e.target.closest(".suggestion"); if (b) send(b.textContent); });
    const ta = $("prompt");
    ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`; updateSend(); });
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
    $("composer").addEventListener("submit", (e) => { e.preventDefault(); const t = ta.value; ta.value = ""; ta.style.height = "auto"; send(t); });

    state.tokens = store.get("frc.tokens");
    if (state.tokens) { try { await accessToken(); await showApp(); return; } catch { state.tokens = null; store.del("frc.tokens"); } }
    showLogin();
  }
  document.addEventListener("DOMContentLoaded", init);
})();
