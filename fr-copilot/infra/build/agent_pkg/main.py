"""FR Copilot agent: Strands Agents on AgentCore Runtime.

- Inbound: Cognito JWT (validated by AgentCore Runtime before we run); Authorization header passed through so
  we can read the rep's username for Memory actor scoping.
- Tools: one catalog from AgentCore Gateway (client-profile-mcp + planning-calc-mcp). Gateway token obtained via
  AgentCore Identity (OAuth2 client-credentials / M2M) - no secrets in code or env.
- Memory: AgentCore Memory short-term (session events) + long-term rep preferences (/preferences/{actorId}).
- Reasoning: Claude adaptive extended thinking; each reasoning block is logged (llm.reasoning).
- Safety: Bedrock Guardrail on every model call; illustration-only disclaimer appended to every answer.
- Streams JSON events (SSE) for the UI: text, tool_start, tool_end, guardrail, done, error.
"""
import base64
import datetime as dt
import json
import os
import re
import time
import uuid

import boto3
from botocore.config import Config
from bedrock_agentcore.identity.auth import requires_access_token
from bedrock_agentcore.memory.integrations.strands.config import AgentCoreMemoryConfig, RetrievalConfig
from bedrock_agentcore.memory.integrations.strands.session_manager import AgentCoreMemorySessionManager
from bedrock_agentcore.runtime import BedrockAgentCoreApp, RequestContext
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient

from jsonlog import get_logger, log_event

MODEL_ID = os.environ["MODEL_ID"]
GATEWAY_URL = os.environ["GATEWAY_URL"]
MEMORY_ID = os.environ["MEMORY_ID"]
GUARDRAIL_ID = os.environ["GUARDRAIL_ID"]
GUARDRAIL_VERSION = os.environ["GUARDRAIL_VERSION"]
IDENTITY_PROVIDER = os.environ["IDENTITY_PROVIDER_NAME"]
GATEWAY_SCOPE = os.environ["GATEWAY_SCOPE"]
DISCLAIMER = "For illustration only, not financial advice. All client data is synthetic."
BLOCKED_MSG = ("I can't help with that request. I can't promise or guarantee investment results, and I can't "
               "process sensitive personal identifiers. " + DISCLAIMER)

APPCONFIG = (os.environ.get("APPCONFIG_APP_ID"), os.environ.get("APPCONFIG_ENV_ID"), os.environ.get("APPCONFIG_PROFILE_ID"))
DEFAULT_MODEL = {"id": MODEL_ID, "name": MODEL_ID, "vendor": "Anthropic", "max_tokens": 8192,
                 "request_fields": {"thinking": {"type": "adaptive"}}}

log = get_logger("fr-copilot-agent")
app = BedrockAgentCoreApp()
_token_cache: dict = {"token": None, "exp": 0}
_llm_cache: dict = {"at": 0.0, "token": None, "model": DEFAULT_MODEL}
_appconfig = boto3.client("appconfigdata", config=Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=2, read_timeout=3))


def active_model() -> dict:
    """Live LLM routing: the active model + its request settings from AppConfig (polled at most every 15 s).

    AppConfig data plane: one configuration session per process; GetLatestConfiguration returns content only when it
    changed. Any failure keeps the last known model (the stack's starting model if we never loaded one)."""
    if not all(APPCONFIG) or time.time() - _llm_cache["at"] < 15:
        return _llm_cache["model"]
    try:
        if not _llm_cache["token"]:
            _llm_cache["token"] = _appconfig.start_configuration_session(
                ApplicationIdentifier=APPCONFIG[0], EnvironmentIdentifier=APPCONFIG[1], ConfigurationProfileIdentifier=APPCONFIG[2],
                RequiredMinimumPollIntervalInSeconds=15)["InitialConfigurationToken"]
        r = _appconfig.get_latest_configuration(ConfigurationToken=_llm_cache["token"])
        _llm_cache["token"] = r["NextPollConfigurationToken"]
        body = r["Configuration"].read()
        if body:
            doc = json.loads(body)
            model = next((c for c in doc["candidates"] if c["id"] == doc["active_model_id"]), None)
            if model and model["id"] != _llm_cache["model"]["id"]:
                log_event(log, "llm.routing.changed", previous=_llm_cache["model"]["id"], model=model["id"],
                          weights=doc.get("weights"), reason=(doc.get("last_change") or {}).get("reason"))
            if model:
                _llm_cache["model"] = model
    except Exception as e:  # keep serving with the last known model
        _llm_cache["token"] = None
        log_event(log, "llm.routing.unavailable", level=30, error=repr(e)[:300])
    _llm_cache["at"] = time.time()
    return _llm_cache["model"]


def _strip_reasoning(messages: list) -> None:
    """Restored history may hold another model's reasoning blocks (with provider signatures); drop them so any of
    the routable models can continue the conversation after a model switch."""
    for m in messages:
        m["content"] = [b for b in m.get("content", []) if "reasoningContent" not in b]
    messages[:] = [m for m in messages if m["content"]]

SYSTEM_PROMPT = f"""You are FR Copilot, an assistant for financial representatives at a fictional life-insurance and
financial-planning firm. Today is {{today}}. All clients are synthetic demo data.

Tools (via one MCP gateway; names are prefixed by server):
- client-profile-mcp___list_clients / get_client_profile / get_policies / get_accounts: client facts. ALWAYS fetch
  data with these tools; never invent client facts.
- planning-calc-mcp___retirement_projection / life_insurance_needs / required_minimum_distribution / savings_gap:
  ALWAYS use these for any number you present; never do financial math yourself.

Method for "on track to retire?": get_client_profile + get_accounts, then retirement_projection with
current_balance = retirement_savings_total, annual_contribution = annual_retirement_contributions_total (includes
employer match), annual_income, income_replacement_pct = desired_retirement_income_pct, social_security_annual =
social_security_estimate_annual, retirement_age as asked (default target_retirement_age). If not on track, call
savings_gap(target_amount = target_nest_egg_nominal, current_savings = the same balance, years =
years_to_retirement, current_annual_contribution = the same contribution) to show the extra saving needed.
Method for "coverage gap?": get_client_profile + get_policies, then life_insurance_needs with method DIME unless
the rep prefers otherwise, debts = other_debt, mortgage_balance, num_children = number of child dependents,
existing_coverage = total_life_death_benefit. If some coverage is non-portable group life, call life_insurance_needs
again with existing_coverage = portable_life_death_benefit and show both gaps. For term policies use expiry_year,
years_remaining and youngest_child_age_at_expiry exactly as returned; never compute dates or ages yourself. If
youngest_child_age_at_expiry is under 22, flag that the term coverage ends before the youngest child likely finishes
college.

Before calling tools, write at most one short sentence. Answer style: start with a one-line verdict per question, then short sections with a compact markdown table of the
key numbers, the main assumptions, and 2-3 suggested next steps for the rep. Use the client's first name.
Apply any remembered preferences of this rep (e.g. preferred method, level of detail, return assumptions).
Never promise, guarantee or imply certain investment returns; describe projections as illustrative.
Do not cite IRS contribution limits, tax brackets or other regulatory figures unless a tool returned them.
Never output Social Security numbers, full account numbers or other sensitive identifiers.
Do not add your own disclaimer line; the application appends one."""


def _claims(ctx: RequestContext) -> dict:
    auth = (ctx.request_headers or {}).get("Authorization", "")
    try:
        payload = auth.split(" ", 1)[1].split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:  # runtime already validated the JWT; claims are only used for scoping
        return {}


def _actor_id(claims: dict) -> str:
    raw = claims.get("username") or claims.get("sub") or "anonymous-rep"
    return re.sub(r"[^a-zA-Z0-9_-]", "-", raw)[:64]


@requires_access_token(provider_name=IDENTITY_PROVIDER, scopes=[GATEWAY_SCOPE], auth_flow="M2M", into="access_token")
async def _fetch_gateway_token(*, access_token: str) -> str:
    return access_token


async def gateway_token() -> str:
    """M2M token from AgentCore Identity (token vault), cached in-process until 60s before expiry."""
    if _token_cache["token"] and time.time() < _token_cache["exp"] - 60:
        return _token_cache["token"]
    tok = await _fetch_gateway_token()
    try:
        body = tok.split(".")[1]
        exp = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))["exp"]
    except Exception:
        exp = time.time() + 300
    _token_cache.update(token=tok, exp=exp)
    return tok


def _split_tool(name: str) -> tuple[str, str]:
    if "___" in name:
        server, tool = name.split("___", 1)
        return server, tool
    return "gateway", name


def _list_all_tools(client: MCPClient):
    tools, token = [], None
    while True:
        page = client.list_tools_sync(pagination_token=token)
        tools.extend(page)
        token = getattr(page, "pagination_token", None)
        if not token:
            return tools


@app.entrypoint
async def invoke(payload: dict, context: RequestContext):
    started = time.perf_counter()
    prompt = (payload or {}).get("prompt", "").strip()
    client_id = (payload or {}).get("client_id", "")
    claims = _claims(context)
    actor = _actor_id(claims)
    session_id = context.session_id or f"adhoc-{uuid.uuid4()}"
    request_id = str(uuid.uuid4())
    log_event(log, "invoke.start", request_id=request_id, actor=actor, session=session_id, client_id=client_id,
              prompt_chars=len(prompt))
    if not prompt:
        yield {"type": "error", "message": "Empty prompt."}
        return
    if client_id and not re.fullmatch(r"C-\d{3}", client_id):
        yield {"type": "error", "message": "Invalid client id."}
        return

    tool_starts: dict = {}
    tool_count = 0
    model_call = 0
    try:
        token = await gateway_token()
        mcp_client = MCPClient(lambda: streamablehttp_client(GATEWAY_URL, headers={"Authorization": f"Bearer {token}"}),
                               startup_timeout=30)
        session_manager = AgentCoreMemorySessionManager(
            AgentCoreMemoryConfig(
                memory_id=MEMORY_ID, session_id=session_id, actor_id=actor,
                retrieval_config={"/preferences/{actorId}": RetrievalConfig(top_k=5, relevance_score=0.3)}),
            region_name=os.environ.get("AWS_REGION", "us-east-1"))
        llm = active_model()
        model_id = llm["id"]
        model = BedrockModel(
            # Model chosen at run time (AppConfig LLM routing). Claude models use extended thinking; reasoning from any
            # model is captured by Bedrock model-invocation logging and logged by us below.
            model_id=model_id, max_tokens=int(llm.get("max_tokens", 8192)), streaming=True,
            **({"additional_request_fields": llm["request_fields"]} if llm.get("request_fields") else {}),
            guardrail_id=GUARDRAIL_ID, guardrail_version=GUARDRAIL_VERSION, guardrail_trace="enabled",
            guardrail_stream_processing_mode="sync", guardrail_latest_message=True,
            guardrail_redact_output=True, guardrail_redact_output_message=BLOCKED_MSG,
            boto_client_config=Config(retries={"max_attempts": 6, "mode": "adaptive"}, read_timeout=120))

        with mcp_client:
            for attempt in range(3):
                try:
                    tools = _list_all_tools(mcp_client)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    time.sleep(0.5 * 2 ** attempt)
            log_event(log, "tools.loaded", request_id=request_id, count=len(tools), model=model_id)
            # Selected client goes in the system prompt (not the user turn) so the Guardrail judges only the rep's words.
            system_prompt = SYSTEM_PROMPT.replace("{today}", dt.date.today().isoformat())
            if client_id:
                system_prompt += (f"\n\nThe rep currently has client {client_id} selected in the UI. 'This client', "
                                  "'she', 'he' or 'they' refer to that client unless the rep names another client id.")
            agent = Agent(model=model, tools=tools, session_manager=session_manager, system_prompt=system_prompt,
                          callback_handler=None,
                          trace_attributes={"session.id": session_id, "actor.id": actor, "fr.client_id": client_id,
                                            "gen_ai.request.model": model_id})
            _strip_reasoning(agent.messages)
            yield {"type": "model", "id": model_id, "name": llm.get("name", model_id), "vendor": llm.get("vendor", "")}
            text = prompt
            ends_with_newline = True
            stop_reason = None
            last_yield = time.perf_counter()
            async for ev in agent.stream_async(text):
                if time.perf_counter() - last_yield > 4:  # keepalive through CloudFront/API GW idle timeouts
                    last_yield = time.perf_counter()
                    yield {"type": "status"}
                if "data" in ev and isinstance(ev["data"], str):
                    last_yield = time.perf_counter()
                    ends_with_newline = ev["data"].endswith("\n")
                    yield {"type": "text", "data": ev["data"]}
                elif "message" in ev:
                    model_call += ev["message"].get("role") == "assistant"
                    for block in ev["message"].get("content", []):
                        if "reasoningContent" in block:
                            rt = (block["reasoningContent"].get("reasoningText") or {}).get("text", "")
                            log_event(log, "llm.reasoning", request_id=request_id, session=session_id, actor=actor,
                                      model=model_id, model_call=model_call, prompt=prompt[:500], reasoning=rt[:8000],
                                      reasoning_chars=len(rt))
                        if "toolUse" in block:
                            tu = block["toolUse"]
                            server, tool = _split_tool(tu["name"])
                            tool_starts[tu["toolUseId"]] = (time.perf_counter(), server, tool)
                            tool_count += 1
                            if not ends_with_newline:  # keep pre-tool narration separate from the answer
                                ends_with_newline = True
                                yield {"type": "text", "data": "\n\n"}
                            yield {"type": "tool_start", "id": tu["toolUseId"], "server": server, "tool": tool,
                                   "input": tu.get("input", {})}
                        elif "toolResult" in block:
                            tr = block["toolResult"]
                            t0, server, tool = tool_starts.get(tr["toolUseId"], (time.perf_counter(), "?", "?"))
                            ms = round((time.perf_counter() - t0) * 1000)
                            log_event(log, "tool.call", request_id=request_id, server=server, tool=tool,
                                      status=tr.get("status"), ms=ms)
                            yield {"type": "tool_end", "id": tr["toolUseId"], "server": server, "tool": tool,
                                   "status": tr.get("status", "success"), "ms": ms}
                elif "result" in ev:
                    stop_reason = str(getattr(ev["result"], "stop_reason", ""))
            if stop_reason == "guardrail_intervened":
                yield {"type": "guardrail", "message": "Bedrock Guardrail intervened."}
            yield {"type": "text", "data": f"\n\n---\n*{DISCLAIMER}*"}
            yield {"type": "done", "stop_reason": stop_reason, "tool_calls": tool_count, "model": model_id,
                   "model_name": llm.get("name", model_id),
                   "ms": round((time.perf_counter() - started) * 1000), "session_id": session_id}
            log_event(log, "invoke.done", request_id=request_id, stop_reason=stop_reason, tool_calls=tool_count, model=model_id,
                      ms=round((time.perf_counter() - started) * 1000))
    except Exception as e:  # never leak internals to the client
        log_event(log, "invoke.error", level=40, request_id=request_id, error=repr(e)[:500])
        yield {"type": "error", "message": f"Sorry, something went wrong (request {request_id[:8]}). Please retry."}


if __name__ == "__main__":
    app.run()
