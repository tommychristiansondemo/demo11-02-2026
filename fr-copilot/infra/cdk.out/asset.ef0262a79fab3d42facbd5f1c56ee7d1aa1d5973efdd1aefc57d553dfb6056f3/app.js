/* FR Copilot Insights SPA: Cognito sign-in/sign-up, streaming chatbot with reasoning + tool-result renderers, knowledge file viewer. */
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const state = { cfg: null, tokens: null, signup: false, busy: false, history: [], knowledge: null };
  const store = {
    get(k) { try { return JSON.parse(sessionStorage.getItem(k)); } catch { return null; } },
    set(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch { /* unavailable */ } },
    del(k) { try { sessionStorage.removeItem(k); } catch { /* ignore */ } },
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n) => (typeof n === "number" ? n.toLocaleString("en-US") : esc(n ?? "-"));
  const when = (t) => { const d = new Date(typeof t === "string" && /^\d{4}-\d\d-\d\d \d/.test(t) ? `${t.replace(" ", "T")}Z` : t); return isNaN(d) ? esc(t) : d.toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", second: "2-digit" }); };

  // ---------- Cognito ----------
  async function cognito(target, body) {
    const r = await fetch(`https://cognito-idp.${state.cfg.region}.amazonaws.com/`, {
      method: "POST", headers: { "Content-Type": "application/x-amz-json-1.1", "X-Amz-Target": `AWSCognitoIdentityProviderService.${target}` }, body: JSON.stringify(body),
    });
    const j = await r.json();
    if (!r.ok) throw new Error((j.message || j.__type || "Request failed").replace(/^PreSignUp failed with error /, ""));
    return j;
  }
  const claims = (jwt) => { try { return JSON.parse(atob(jwt.split(".")[1].replace(/-/g, "+").replace(/_/g, "/"))); } catch { return {}; } };
  function setTokens(ar) {
    const prev = state.tokens || {};
    state.tokens = { access: ar.AccessToken, refresh: ar.RefreshToken || prev.refresh, exp: claims(ar.AccessToken).exp || 0 };
    store.set("fri.tokens", state.tokens);
  }
  async function login(u, p) {
    const j = await cognito("InitiateAuth", { AuthFlow: "USER_PASSWORD_AUTH", ClientId: state.cfg.clientId, AuthParameters: { USERNAME: u, PASSWORD: p } });
    if (!j.AuthenticationResult) throw new Error("Additional sign-in challenge required.");
    setTokens(j.AuthenticationResult);
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
    state.tokens = null; store.del("fri.tokens"); location.reload();
  }
  async function api(path, opts = {}) {
    const r = await fetch(path, { ...opts, headers: { ...(opts.headers || {}), Authorization: `Bearer ${await accessToken()}` } });
    if (r.status === 401) { showLogin("Your session expired. Please sign in again."); throw new Error("unauthorized"); }
    return r;
  }

  // ---------- Views ----------
  function setMode(signup) {
    state.signup = signup;
    $("mode-signin").classList.toggle("active", !signup); $("mode-signin").setAttribute("aria-selected", String(!signup));
    $("mode-signup").classList.toggle("active", signup); $("mode-signup").setAttribute("aria-selected", String(signup));
    $("confirm-wrap").hidden = !signup; $("signup-help").hidden = !signup; $("password2").required = signup;
    $("password").autocomplete = signup ? "new-password" : "current-password";
    $("login-btn").textContent = signup ? "Create account and sign in" : "Sign in";
    $("login-error").hidden = true;
  }
  function showLogin(msg) {
    $("app-view").hidden = true; $("login-view").hidden = false;
    if (msg) { $("login-error").textContent = msg; $("login-error").hidden = false; }
    $("username").focus();
  }
  function showApp() {
    $("login-view").hidden = true; $("app-view").hidden = false;
    $("whoami").textContent = claims(state.tokens.access).username || "";
    $("prompt").focus();
  }
  function switchView(v) {
    document.querySelectorAll(".tab").forEach((t) => { const on = t.dataset.view === v; t.classList.toggle("active", on); t.setAttribute("aria-selected", String(on)); });
    $("view-ask").hidden = v !== "ask"; $("view-knowledge").hidden = v !== "knowledge";
    if (v === "knowledge" && !state.knowledge) loadKnowledge();
  }

  // ---------- Knowledge file ----------
  async function loadKnowledge() {
    $("kn-body").innerHTML = '<p class="muted">Loading…</p>';
    try {
      const j = await (await api("/api/knowledge")).json();
      if (j.error) throw new Error(j.error);
      state.knowledge = j;
      $("kn-meta").innerHTML = `s3://${esc(j.bucket)}/${esc(j.key)} · ${num(j.bytes)} bytes · updated ${when(j.last_modified)} · version <code>${esc((j.version_id || "").slice(0, 12))}</code>`;
      $("kn-versions").innerHTML = (j.history || []).map((v, i) => `<li>${when(v.last_modified)} · ${num(v.bytes)} bytes · <code>${esc(v.version_id.slice(0, 12))}</code>${i === 0 ? ' <span class="pill">current</span>' : ""}</li>`).join("");
      $("kn-body").innerHTML = renderMd(j.markdown);
    } catch (e) {
      if (e.message !== "unauthorized") $("kn-body").innerHTML = `<p class="error">Could not load the knowledge file: ${esc(e.message)}</p>`;
    }
  }
  function downloadKnowledge() {
    if (!state.knowledge) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([state.knowledge.markdown], { type: "text/markdown" }));
    a.download = "projectknowledge.md"; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  // ---------- Chat ----------
  function updateSend() { $("send").disabled = state.busy || !$("prompt").value.trim(); }
  function scrollDown() { const t = $("thread"); t.scrollTop = t.scrollHeight; }
  async function ask(text) {
    if (state.busy || !text.trim()) return;
    state.busy = true; updateSend();
    $("welcome")?.remove();
    const u = document.createElement("div"); u.className = "msg user"; u.textContent = text; $("thread").appendChild(u);
    const bot = document.createElement("div"); bot.className = "msg bot";
    bot.innerHTML = `<details class="reasoning" open hidden><summary><span class="think-dot"></span> Model reasoning <span class="r-count muted small"></span></summary><pre class="r-text"></pre></details>
      <div class="steps"></div><div class="md answer"><span class="typing"><i></i><i></i><i></i></span></div><div class="meta"></div>`;
    $("thread").appendChild(bot); scrollDown();
    const rBox = bot.querySelector(".reasoning"); const rText = bot.querySelector(".r-text"); const steps = bot.querySelector(".steps"); const ans = bot.querySelector(".answer");
    let md = ""; let reasoning = ""; let done = null; let failed = null; const cards = {};
    try {
      const r = await api("/api/chat", { method: "POST", headers: { "Content-Type": "application/json", Accept: "text/event-stream" }, body: JSON.stringify({ prompt: text, history: state.history.slice(-12) }) });
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
            if (ev.type === "reasoning") {
              reasoning += ev.data; rBox.hidden = false; rText.textContent = reasoning;
              bot.querySelector(".r-count").textContent = `${reasoning.length.toLocaleString()} chars`; rText.scrollTop = rText.scrollHeight;
            } else if (ev.type === "text") { md += ev.data; ans.innerHTML = renderMd(md); }
            else if (ev.type === "tool_start") { cards[ev.id] = toolCard(ev); steps.appendChild(cards[ev.id]); }
            else if (ev.type === "tool_result") fillTool(cards[ev.id], ev);
            else if (ev.type === "error") failed = ev.message;
            else if (ev.type === "done") done = ev;
            scrollDown();
          }
        }
      }
    } catch (e) { if (e.message !== "unauthorized") failed = "Network error. Please retry."; }
    rBox.open = false; rBox.classList.add("finished");
    if (!md) ans.innerHTML = failed ? `<p class="error">${esc(failed)}</p>` : '<p class="muted">(no answer)</p>';
    else if (failed) ans.insertAdjacentHTML("beforeend", `<p class="error">${esc(failed)}</p>`);
    if (done) {
      const u2 = done.usage || {};
      bot.querySelector(".meta").textContent = `${done.model.replace(/^us\.anthropic\./, "")} · ${done.tool_calls} tool call${done.tool_calls === 1 ? "" : "s"} · ${num(u2.inputTokens)} in / ${num(u2.outputTokens)} out tokens${u2.cacheReadInputTokens ? ` (${num(u2.cacheReadInputTokens)} cached)` : ""} · ${(done.ms / 1000).toFixed(1)} s`;
    }
    if (md) { state.history.push({ role: "user", text }, { role: "assistant", text: md }); }
    state.busy = false; updateSend(); $("prompt").focus();
  }

  // ---------- Tool cards ----------
  const TOOL_LABEL = { query_logs: "CloudWatch Logs Insights", get_llm_invocations: "Bedrock invocation logs", get_deployed_resources: "CloudFormation inventory", get_bedrock_usage: "Bedrock metrics" };
  function toolCard(ev) {
    const el = document.createElement("details"); el.className = "tool"; el.open = true;
    el.innerHTML = `<summary><span class="tchip">${esc(ev.name)}</span><span class="muted small">${esc(TOOL_LABEL[ev.name] || "tool")}</span><span class="args">${esc(JSON.stringify(ev.input))}</span><span class="status run">running…</span></summary><div class="tbody"></div>`;
    return el;
  }
  function fillTool(el, ev) {
    if (!el) return;
    const st = el.querySelector(".status"); st.className = `status ${ev.status === "success" ? "ok" : "err"}`; st.textContent = `${(ev.ms / 1000).toFixed(1)} s`;
    const body = el.querySelector(".tbody"); const r = ev.result || {};
    try {
      if (r.error) body.innerHTML = `<p class="error">${esc(r.error)}</p>`;
      else if (ev.name === "query_logs") body.innerHTML = renderLogs(r);
      else if (ev.name === "get_llm_invocations") body.innerHTML = renderInvocations(r);
      else if (ev.name === "get_deployed_resources") body.innerHTML = renderInventory(r);
      else if (ev.name === "get_bedrock_usage") body.innerHTML = renderUsage(r);
      else body.innerHTML = `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`;
    } catch { body.innerHTML = `<pre>${esc(JSON.stringify(r, null, 2))}</pre>`; }
  }
  function msgCell(m) {
    if (m && typeof m === "object") {
      const head = [m.level, m.msg || m.event || m.name].filter(Boolean).map(esc).join(" · ");
      const rest = { ...m }; for (const k of ["ts", "level", "msg", "service", "timestamp"]) delete rest[k];
      return `${head ? `<b>${head}</b> ` : ""}<code class="kv">${esc(JSON.stringify(rest))}</code>`;
    }
    return `<code class="kv">${esc(m)}</code>`;
  }
  function renderLogs(r) {
    const rows = r.rows || [];
    const head = `<div class="tmeta">${rows.length} row${rows.length === 1 ? "" : "s"} · last ${num(r.minutes)} min · ${esc((r.log_groups || []).join(", "))}${r.note ? ` · ${esc(r.note)}` : ""}</div><details class="q"><summary>Logs Insights query</summary><pre>${esc(r.query || "")}</pre></details>`;
    if (!rows.length) return `${head}<p class="muted small">No matching log events.</p>`;
    return `${head}<div class="tscroll"><table class="data"><thead><tr><th>Time</th><th>Source</th><th>Event</th></tr></thead><tbody>${rows.map((x) => `<tr><td class="nowrap">${when(x.timestamp)}</td><td><span class="src">${esc(x.source)}</span></td><td>${msgCell(x.message)}</td></tr>`).join("")}</tbody></table></div>`;
  }
  function renderInvocations(r) {
    const inv = r.invocations || [];
    if (!inv.length) return `<p class="muted small">No logged LLM calls in the window (${esc(r.app)}, last ${num(r.minutes)} min).</p>`;
    return `<div class="tmeta">${inv.length} of ${num(r.matched)} logged model calls · ${esc(r.log_group)}</div>` + inv.map((x) => {
      const lt = x.prompt?.latest_turn || {};
      const latest = lt.kind === "tool_results"
        ? `<ul class="tr">${(lt.tool_results || []).map((t) => `<li><code>${esc(t.status || "")}</code> ${esc(t.content)}</li>`).join("")}</ul>`
        : `<p>${esc(lt.text || "")}</p>`;
      return `<article class="inv">
        <header><span class="app-chip ${esc(x.app)}">${esc(x.app)}</span><span>${when(x.timestamp)}</span><span class="muted small">${esc((x.model_id || "").replace(/^.*anthropic\./, ""))}</span>
          <span class="muted small">${num(x.input_tokens)} in / ${num(x.output_tokens)} out${x.latency_ms ? ` · ${num(x.latency_ms)} ms` : ""}${x.stop_reason ? ` · ${esc(x.stop_reason)}` : ""}</span></header>
        <div class="inv-grid">
          <div><h4>User question</h4><p>${esc(x.prompt?.user_question || "")}</p>
            <h4>This call's input (${esc(lt.kind || "")}, ${num(x.prompt?.conversation_messages)} messages)</h4>${latest}
            <p class="muted small">System prompt ${num(x.prompt?.system_prompt_chars)} chars · thinking ${esc(JSON.stringify(x.prompt?.thinking))}${x.prompt?.guardrail ? ` · guardrail ${esc(x.prompt.guardrail.id)} v${esc(x.prompt.guardrail.version)}` : ""}</p></div>
          <div><h4>Reasoning</h4><pre class="r-log">${esc(x.reasoning)}</pre></div>
        </div>
        ${x.tool_calls_requested?.length ? `<h4>Tool calls requested</h4><div class="calls">${x.tool_calls_requested.map((t) => `<span class="call"><b>${esc(t.name)}</b> <code>${esc(JSON.stringify(t.input))}</code></span>`).join("")}</div>` : ""}
        ${x.response_text ? `<h4>Response text</h4><div class="md small-md">${renderMd(x.response_text)}</div>` : ""}
      </article>`;
    }).join("");
  }
  function renderInventory(r) {
    return `<div class="tmeta">${num(r.total_resources)} resources · ${num(r.distinct_types_across_stacks)} distinct resource types across stacks</div>` + (r.stacks || []).map((s) => `
      <h4>${esc(s.stack)} <span class="muted small">${esc(s.status)} · ${num(s.total_resources)} resources · ${num(s.distinct_types)} types</span></h4>
      <div class="tscroll short"><table class="data"><thead><tr><th>Resource type</th><th class="r">Count</th></tr></thead><tbody>${s.by_type.map((t) => `<tr><td><code>${esc(t.type)}</code></td><td class="r">${t.count}</td></tr>`).join("")}</tbody></table></div>
      <details><summary>All ${num(s.total_resources)} resources</summary><div class="tscroll"><table class="data"><thead><tr><th>Logical id</th><th>Type</th><th>Physical id</th></tr></thead><tbody>${s.resources.map((x) => `<tr><td>${esc(x.logical_id)}</td><td><code>${esc(x.type)}</code></td><td class="break">${esc(x.physical_id)}</td></tr>`).join("")}</tbody></table></div></details>`).join("");
  }
  function renderUsage(r) {
    const cols = ["Invocations", "InputTokenCount", "OutputTokenCount", "InvocationLatency", "InvocationClientErrors", "InvocationServerErrors", "InvocationThrottles"];
    return `<div class="tmeta">Last ${num(r.hours)} h · AWS/Bedrock metrics</div><div class="tscroll"><table class="data"><thead><tr><th>App</th><th>Model</th>${cols.map((c) => `<th class="r">${esc(c.replace("Invocation", "").replace("Count", "") || "Invocations")}</th>`).join("")}</tr></thead><tbody>${(r.usage || []).map((u) => `<tr><td>${esc(u.app)}</td><td><code>${esc(u.model_id)}</code></td>${cols.map((c) => `<td class="r">${num(u[c] ?? 0)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
  }

  // ---------- Safe markdown (escape first, then format) ----------
  function inline(s) {
    const codes = []; // protect code spans from emphasis/link rules
    s = s.replace(/`([^`]+)`/g, (_, c) => `\u0000${codes.push(c) - 1}\u0000`);
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
      .replace(/\[([^\]]+)\]\((https:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
      .replace(/(^|[\s(])(https:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, n) => `<code>${codes[Number(n)]}</code>`);
  }
  function renderMd(src) {
    const lines = esc(src).split("\n"); const out = []; let i = 0;
    const isRow = (l) => /^\s*\|.*\|\s*$/.test(l);
    const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
    const listRe = /^(\s*)([-*•]|\d+[.)])\s+/;
    while (i < lines.length) {
      const l = lines[i]; let m;
      if (/^\s*```/.test(l)) {
        const code = []; i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) code.push(lines[i++]);
        i++; out.push(`<pre class="code">${code.join("\n")}</pre>`); continue;
      }
      if (isRow(l) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const head = cells(l); i += 2; const body = [];
        while (i < lines.length && isRow(lines[i])) body.push(cells(lines[i++]));
        out.push(`<div class="tscroll"><table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${r.map((c) => `<td>${c.replace(/&lt;br&gt;/g, "<br>")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      if ((m = l.match(/^(#{1,4})\s+(.*)$/))) { const h = Math.min(m[1].length + 1, 5); out.push(`<h${h}>${inline(m[2])}</h${h}>`); i++; continue; }
      if (/^\s*(-{3,}|\*{3,})\s*$/.test(l)) { out.push("<hr>"); i++; continue; }
      if ((m = l.match(/^&gt;\s?(.*)$/))) { const q = []; while (i < lines.length && /^&gt;/.test(lines[i])) q.push(inline(lines[i++].replace(/^&gt;\s?/, ""))); out.push(`<blockquote>${q.join("<br>")}</blockquote>`); continue; }
      if (listRe.test(l)) {
        const ordered = /\d/.test(l.match(listRe)[2]); const items = [];
        while (i < lines.length && (listRe.test(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
          const ln = lines[i++];
          if (listRe.test(ln)) { const ind = ln.match(listRe)[1].length; items.push(`<li${ind >= 2 ? ' class="sub"' : ""}>${inline(ln.replace(listRe, ""))}`); }
          else items[items.length - 1] += ` ${inline(ln.trim())}`;
        }
        out.push(`<${ordered ? "ol" : "ul"}>${items.map((x) => `${x}</li>`).join("")}</${ordered ? "ol" : "ul"}>`); continue;
      }
      if (!l.trim()) { i++; continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !isRow(lines[i]) && !/^(#{1,4})\s|^\s*```|^&gt;/.test(lines[i]) && !listRe.test(lines[i]) && !/^\s*-{3,}\s*$/.test(lines[i])) para.push(inline(lines[i++]));
      if (para.length) out.push(`<p>${para.join(" ")}</p>`); else i++;
    }
    return out.join("");
  }

  // ---------- Wiring ----------
  async function init() {
    try { state.cfg = await (await fetch("config.json", { cache: "no-store" })).json(); } catch { document.body.textContent = "Configuration unavailable."; return; }
    if (state.cfg.appUrl) document.querySelectorAll(".app-link").forEach((a) => { a.href = state.cfg.appUrl; });
    $("mode-signin").addEventListener("click", () => setMode(false));
    $("mode-signup").addEventListener("click", () => setMode(true));
    $("login-form").addEventListener("submit", async (e) => {
      e.preventDefault(); $("login-error").hidden = true; $("login-btn").disabled = true;
      const u = $("username").value.trim(); const pw = $("password").value;
      try {
        if (state.signup) {
          if (!/^[A-Za-z0-9][A-Za-z0-9._-]{2,31}$/.test(u)) throw new Error("Username must be 3-32 letters, digits, '.', '_' or '-'.");
          if (pw.length < 6) throw new Error("Password must be at least 6 characters.");
          if (pw !== $("password2").value) throw new Error("Passwords do not match.");
          await cognito("SignUp", { ClientId: state.cfg.clientId, Username: u, Password: pw });
        }
        await login(u, pw); $("password").value = ""; $("password2").value = ""; showApp();
      } catch (err) { $("login-error").textContent = err.message; $("login-error").hidden = false; }
      finally { $("login-btn").disabled = false; }
    });
    $("logout").addEventListener("click", logout);
    document.querySelectorAll(".tab").forEach((t) => t.addEventListener("click", () => switchView(t.dataset.view)));
    $("kn-refresh").addEventListener("click", () => { state.knowledge = null; loadKnowledge(); });
    $("kn-download").addEventListener("click", downloadKnowledge);
    $("thread").addEventListener("click", (e) => { const b = e.target.closest(".suggestion"); if (b) ask(b.textContent); });
    const ta = $("prompt");
    ta.addEventListener("input", () => { ta.style.height = "auto"; ta.style.height = `${Math.min(ta.scrollHeight, 160)}px`; updateSend(); });
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("composer").requestSubmit(); } });
    $("composer").addEventListener("submit", (e) => { e.preventDefault(); const t = ta.value; ta.value = ""; ta.style.height = "auto"; ask(t); });

    state.tokens = store.get("fri.tokens");
    if (state.tokens) { try { await accessToken(); showApp(); return; } catch { state.tokens = null; store.del("fri.tokens"); } }
    showLogin();
  }
  document.addEventListener("DOMContentLoaded", init);
})();
