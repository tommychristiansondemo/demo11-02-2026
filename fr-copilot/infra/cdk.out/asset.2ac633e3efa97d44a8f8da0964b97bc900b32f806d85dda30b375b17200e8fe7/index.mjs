// FR Copilot chat proxy: API Gateway (REST, response streaming) -> AgentCore Runtime.
// API Gateway's Cognito authorizer has already validated the rep's access token; we forward the SAME bearer token
// so AgentCore Runtime's inbound JWT authorizer validates it again (defense in depth). No AWS credentials are used
// for the runtime call. Retries happen only before the first byte is streamed.
const REGION = process.env.AWS_REGION;
const RUNTIME_ARN = process.env.AGENT_RUNTIME_ARN;
const ENDPOINT = `https://bedrock-agentcore.${REGION}.amazonaws.com/runtimes/${encodeURIComponent(RUNTIME_ARN)}/invocations?qualifier=DEFAULT`;
const RETRYABLE = new Set([429, 500, 502, 503, 504]);
const SESSION_RE = /^[A-Za-z0-9-]{33,100}$/;
const CLIENT_RE = /^C-\d{3}$/;

const log = (level, msg, fields = {}) =>
  console.log(JSON.stringify({ ts: new Date().toISOString(), level, service: "frcopilot-chat-proxy", msg, ...fields }));

const sse = (obj) => `data: ${JSON.stringify(obj)}\n\n`;

function open(responseStream, statusCode) {
  return awslambda.HttpResponseStream.from(responseStream, {
    statusCode,
    headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-cache, no-store", "X-Content-Type-Options": "nosniff" },
  });
}

function fail(responseStream, statusCode, message) {
  const s = open(responseStream, statusCode);
  s.write(sse({ type: "error", message }));
  s.end();
}

async function invokeWithRetry(body, auth, sessionId, requestId) {
  let lastErr;
  for (let attempt = 1; attempt <= 3; attempt++) {
    try {
      const resp = await fetch(ENDPOINT, {
        method: "POST",
        headers: {
          Authorization: auth,
          "Content-Type": "application/json",
          Accept: "text/event-stream",
          "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": sessionId,
          "X-Amzn-Trace-Id": `Root=1-${Math.floor(Date.now() / 1000).toString(16)}-${requestId.replace(/-/g, "").slice(0, 24)}`,
        },
        body,
        signal: AbortSignal.timeout(280_000),
      });
      if (!RETRYABLE.has(resp.status) || attempt === 3) return resp;
      lastErr = new Error(`runtime status ${resp.status}`);
      log("WARN", "runtime.retry", { requestId, attempt, status: resp.status });
    } catch (e) {
      lastErr = e;
      log("WARN", "runtime.retry", { requestId, attempt, error: String(e) });
      if (attempt === 3) throw e;
    }
    await new Promise((r) => setTimeout(r, 250 * 2 ** attempt + Math.random() * 200));
  }
  throw lastErr;
}

export const handler = awslambda.streamifyResponse(async (event, responseStream, context) => {
  const requestId = context.awsRequestId;
  const t0 = Date.now();
  const headers = Object.fromEntries(Object.entries(event.headers || {}).map(([k, v]) => [k.toLowerCase(), v]));
  const auth = headers.authorization || "";
  const user = event.requestContext?.authorizer?.claims?.username || "unknown";
  let payload;
  try {
    const raw = event.isBase64Encoded ? Buffer.from(event.body || "", "base64").toString() : event.body || "";
    payload = JSON.parse(raw);
  } catch {
    return fail(responseStream, 400, "Request body must be JSON.");
  }
  const { prompt, client_id: clientId = "", session_id: sessionId } = payload;
  if (typeof prompt !== "string" || !prompt.trim() || prompt.length > 4000) return fail(responseStream, 400, "prompt must be 1-4000 characters.");
  if (clientId && !CLIENT_RE.test(clientId)) return fail(responseStream, 400, "Invalid client_id.");
  if (!SESSION_RE.test(sessionId || "")) return fail(responseStream, 400, "Invalid session_id.");
  if (!auth.startsWith("Bearer ")) return fail(responseStream, 401, "Missing bearer token.");

  log("INFO", "chat.start", { requestId, user, clientId, sessionId, promptChars: prompt.length });
  let resp;
  try {
    resp = await invokeWithRetry(JSON.stringify({ prompt, client_id: clientId }), auth, sessionId, requestId);
  } catch (e) {
    log("ERROR", "runtime.unreachable", { requestId, error: String(e) });
    return fail(responseStream, 502, "The assistant is temporarily unavailable. Please retry.");
  }
  if (!resp.ok) {
    const detail = (await resp.text()).slice(0, 300);
    log("ERROR", "runtime.error", { requestId, status: resp.status, detail });
    return fail(responseStream, resp.status === 401 || resp.status === 403 ? 401 : 502,
      resp.status === 401 || resp.status === 403 ? "Session expired. Please sign in again." : "The assistant returned an error. Please retry.");
  }
  const out = open(responseStream, 200);
  let bytes = 0;
  try {
    for await (const chunk of resp.body) {
      bytes += chunk.length;
      out.write(chunk);
    }
  } catch (e) {
    log("ERROR", "stream.broken", { requestId, error: String(e), bytes });
    out.write(sse({ type: "error", message: "The response stream was interrupted. Please retry." }));
  }
  out.end();
  log("INFO", "chat.done", { requestId, user, bytes, ms: Date.now() - t0 });
});
