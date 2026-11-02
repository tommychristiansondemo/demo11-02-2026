"""Checks for release 2 (self sign-up + Insights app). Uses IAM-only calls: Lambdas are invoked directly with a
synthetic API Gateway event, so no user password is needed. No account is created.
Usage: python scripts/insights_test.py
"""
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import boto3

REGION = "us-east-1"
INSIGHTS = "https://insights.awsteach.com"
APP = "https://www.awsteach.com"
lam = boto3.client("lambda", region_name=REGION)
results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"  ({detail})" if detail else ""))


def http(url, method="GET", body=None):
    req = urllib.request.Request(url, method=method, data=body, headers={"Content-Type": "application/json"} if body else {})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def api_event(body=None):
    return {"httpMethod": "POST" if body else "GET", "body": json.dumps(body) if body else None, "isBase64Encoded": False,
            "headers": {}, "requestContext": {"authorizer": {"claims": {"username": "insights-test"}}}}


def chat(prompt):
    r = lam.invoke_with_response_stream(FunctionName="frcopilot-insights-chat", Payload=json.dumps(api_event({"prompt": prompt})))
    raw = b"".join(e["PayloadChunk"]["Payload"] for e in r["EventStream"] if "PayloadChunk" in e).decode()
    events = []
    for line in raw.split("\n"):
        if line.startswith("data:"):
            try:
                events.append(json.loads(line[5:]))
            except json.JSONDecodeError:
                pass
    return events


def main():
    out = {o["OutputKey"]: o["OutputValue"] for o in boto3.client("cloudformation", region_name=REGION)
           .describe_stacks(StackName="FRCopilot")["Stacks"][0]["Outputs"]}
    # --- Self sign-up configuration ---
    pool = boto3.client("cognito-idp", region_name=REGION).describe_user_pool(UserPoolId=out["UserPoolId"])["UserPool"]
    pp = pool["Policies"]["PasswordPolicy"]
    check("self sign-up enabled", not pool["AdminCreateUserConfig"]["AllowAdminCreateUserOnly"])
    check("minimum password policy (6, no char classes)", pp["MinimumLength"] == 6 and not any(pp[k] for k in ("RequireUppercase", "RequireLowercase", "RequireNumbers", "RequireSymbols")), json.dumps(pp))
    check("PreSignUp trigger attached", "frcopilot-presignup" in (pool.get("LambdaConfig") or {}).get("PreSignUp", ""))
    ev = {"userName": "student.test-1", "request": {"userAttributes": {}}, "response": {}, "callerContext": {"clientId": "x"}}
    r = json.loads(lam.invoke(FunctionName="frcopilot-presignup", Payload=json.dumps(ev))["Payload"].read())
    check("PreSignUp auto-confirms (no email/code)", r.get("response", {}).get("autoConfirmUser") is True)
    bad = lam.invoke(FunctionName="frcopilot-presignup", Payload=json.dumps({**ev, "userName": "x"}))
    check("PreSignUp rejects invalid username", "FunctionError" in bad)

    # --- Sites and auth ---
    s, h, body = http(INSIGHTS)
    check("insights site 200 with CSP", s == 200 and "content-security-policy" in {k.lower() for k in h}, f"HTTP {s}")
    s, _, _ = http(f"{INSIGHTS}/api/chat", "POST", b'{"prompt":"hi"}')
    check("insights chat API rejects no token", s == 401, f"HTTP {s}")
    s, _, _ = http(f"{INSIGHTS}/api/knowledge")
    check("insights knowledge API rejects no token", s == 401, f"HTTP {s}")
    s, _, cfg = http(f"{APP}/config.json")
    check("main app config links to Insights", s == 200 and json.loads(cfg).get("insightsUrl") == INSIGHTS)
    s, _, html = http(f"{APP}/")
    check("main page has Insights link + sign-up tab", "insights-link" in html and "mode-signup" in html)

    # --- Knowledge file ---
    k = json.loads(json.loads(lam.invoke(FunctionName="frcopilot-insights-knowledge", Payload=json.dumps(api_event()))["Payload"].read())["body"])
    md = k.get("markdown", "")
    check("projectknowledge.md served from S3", len(md) > 5000 and "Live deployment facts" in md, f"{len(md)} chars, version {k.get('version_id')}")
    sm = boto3.client("secretsmanager", region_name=REGION)
    leaked = []
    for name in ("frcopilot/demo-user/rep.jordan", "frcopilot/demo-user/rep.casey", "frcopilot/agent-to-gateway-client", "frcopilot/gateway-to-mcp-client"):
        sec = json.loads(sm.get_secret_value(SecretId=name)["SecretString"])
        for key in ("password", "client_secret"):
            if sec.get(key) and sec[key] in md:
                leaked.append(f"{name}:{key}")
    check("no secret values in knowledge file", not leaked, ", ".join(leaked))

    # --- Invocation logging ---
    cfgl = boto3.client("bedrock", region_name=REGION).get_model_invocation_logging_configuration().get("loggingConfig") or {}
    check("Bedrock model-invocation logging on", cfgl.get("cloudWatchConfig", {}).get("logGroupName") == "/aws/bedrock/frcopilot-model-invocations")

    # --- Chatbot ---
    evs = chat("How many distinct AWS resource types are in use across both stacks? Use the live inventory tool.")
    tools = [e["name"] for e in evs if e.get("type") == "tool_start"]
    text = "".join(e["data"] for e in evs if e.get("type") == "text")
    check("chatbot answers inventory question with live tool", "get_deployed_resources" in tools and any(e.get("type") == "done" for e in evs), f"tools={tools}")
    check("chatbot streams its reasoning", any(e.get("type") == "reasoning" for e in evs))
    evs = chat("Where does FR Copilot get its client data?")
    text = "".join(e["data"] for e in evs if e.get("type") == "text")
    check("chatbot answers design question from knowledge file", "synthetic" in text.lower() and "dynamodb" in text.lower(), text[:120].replace("\n", " "))
    evs = chat("Show the reasoning from the last 2 insights chatbot LLM calls.")
    res = [e for e in evs if e.get("type") == "tool_result" and e["name"] == "get_llm_invocations"]
    inv = res[0]["result"].get("invocations", []) if res else []
    has_reasoning = any(i.get("reasoning") and not i["reasoning"].startswith("(no reasoning") for i in inv)
    check("reasoning retrieved from Bedrock invocation logs", has_reasoning, f"{len(inv)} invocations")
    raw = json.dumps(evs)
    check("no JWT/keys in chatbot stream", "eyJ" not in raw and "AKIA" not in raw)

    # --- LLM routing (AppConfig) ---
    def selector(method, body=None):
        ev = {**api_event(body), "httpMethod": method}
        r = json.loads(lam.invoke(FunctionName="frcopilot-llm-selector", Payload=json.dumps(ev))["Payload"].read())
        return r["statusCode"], json.loads(r["body"])
    code, g = selector("GET")
    check("routing GET: 8 ranked models from >=4 vendors", code == 200 and len(g["ranking"]) == 8 and len({r["vendor"] for r in g["ranking"]}) >= 4,
          f"active={g.get('active_model_name')} weights={g.get('weights')}")
    code, b = selector("POST", {"quality": 60, "cost": 30, "latency": 30})
    check("routing rejects weights not summing to 100", code == 400, b.get("error", ""))
    code, p = selector("POST", {"quality": 100, "cost": 0, "latency": 0})
    check("quality-only preference switches live model to Opus 4.6", code == 200 and p["active_model_id"] == "us.anthropic.claude-opus-4-6-v1" and p["ranking"][0]["name"] == "Claude Opus 4.6",
          p.get("decision", {}).get("reason", "")[:100])
    code, p = selector("POST", {"quality": 20, "cost": 60, "latency": 20})
    top = p["ranking"][0]
    check("cost-heavy preference picks best ELIGIBLE model", code == 200 and p["active_model_id"] == next(r["id"] for r in p["ranking"] if r["eligible"]),
          f"#1 {top['name']} (eligible={top['eligible']}) -> live {p['active_model_name']}")
    # agent's own AppConfig loader sees the change (same code path the runtime uses)
    import importlib.util, os, time as _t
    o = {x["OutputKey"]: x["OutputValue"] for x in boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName="FRCopilot")["Stacks"][0]["Outputs"]}
    os.environ.update({"MODEL_ID": o["AgentModelId"], "GATEWAY_URL": "x", "MEMORY_ID": "x", "GUARDRAIL_ID": "x", "GUARDRAIL_VERSION": "1",
                       "IDENTITY_PROVIDER_NAME": "x", "GATEWAY_SCOPE": "x", "APPCONFIG_APP_ID": o["AppConfigAppId"],
                       "APPCONFIG_ENV_ID": o["AppConfigEnvId"], "APPCONFIG_PROFILE_ID": o["AppConfigProfileId"], "AWS_DEFAULT_REGION": REGION})
    root = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(root / "services/shared")]
    spec = importlib.util.spec_from_file_location("agent_main", root / "services/agent/main.py")
    agent = importlib.util.module_from_spec(spec); spec.loader.exec_module(agent)
    seen = None
    for _ in range(8):
        agent._llm_cache["at"] = 0
        seen = agent.active_model()["id"]
        if seen == p["active_model_id"]:
            break
        _t.sleep(4)
    check("agent runtime loader picks up the new model from AppConfig", seen == p["active_model_id"], seen)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
