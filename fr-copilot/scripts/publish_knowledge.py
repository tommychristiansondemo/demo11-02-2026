"""Build projectknowledge.md (hand-written narrative + live facts from AWS + facts parsed from source) and upload it
to the versioned knowledge bucket. Run after every deployment (deploy.sh does this).

Never writes secret values: only names/ARNs. A final redaction pass strips anything token- or key-shaped.
Usage: python scripts/publish_knowledge.py [--dry-run]
"""
import ast
import datetime as dt
import json
import re
import sys
from collections import Counter
from pathlib import Path

import boto3

REGION = "us-east-1"
ROOT = Path(__file__).resolve().parents[1]
STACKS = ["FRCopilot", "FRCopilotInsights"]
cf = boto3.client("cloudformation", region_name=REGION)


def outputs(stack):
    return {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName=stack)["Stacks"][0].get("Outputs", [])}


def resources(stack):
    res = []
    for page in cf.get_paginator("list_stack_resources").paginate(StackName=stack):
        res += page["StackResourceSummaries"]
    return res


def mcp_tools(path: Path):
    """(name, signature, docstring) for every @mcp.tool() function in an MCP server."""
    tree = ast.parse(path.read_text())
    out = []
    for fn in tree.body:
        if isinstance(fn, ast.FunctionDef) and any("tool" in ast.unparse(d) for d in fn.decorator_list):
            out.append((fn.name, ast.unparse(fn.args), (ast.get_docstring(fn) or "").split("\n")[0]))
    return out


def agent_system_prompt():
    tree = ast.parse((ROOT / "services/agent/main.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SYSTEM_PROMPT":
            v = node.value
            if isinstance(v, ast.JoinedStr):
                return "".join(p.value if isinstance(p, ast.Constant) else "{" + ast.unparse(p.value) + "}" for p in v.values)
            return v.value
    return "(not found)"


def insights_system_prompt():
    src = (ROOT / "lambda/insights_chat/index.mjs").read_text()
    m = re.search(r"const SYSTEM = `(.*?)`;", src, re.S)
    return m.group(1) if m else "(not found)"


def guardrail_section(gid):
    g = boto3.client("bedrock", region_name=REGION).get_guardrail(guardrailIdentifier=gid)
    lines = [f"- Name `{g['name']}`, id `{g['guardrailId']}`, latest version `{g['version']}` (status {g['status']})"]
    for t in g.get("topicPolicy", {}).get("topics", []):
        lines.append(f"- Denied topic **{t['name']}**: {t['definition']}")
    for p in g.get("sensitiveInformationPolicy", {}).get("piiEntities", []):
        lines.append(f"- PII `{p['type']}` → {p['action']}")
    for f in g.get("contentPolicy", {}).get("filters", []):
        lines.append(f"- Content filter `{f['type']}`: input {f['inputStrength']}, output {f['outputStrength']}")
    lines.append(f"- Blocked message: \"{g.get('blockedInputMessaging', '')}\"")
    return "\n".join(lines)


def iam_section():
    iam = boto3.client("iam")
    lines = []
    for page in iam.get_paginator("list_roles").paginate():
        for r in page["Roles"]:
            n = r["RoleName"]
            if not (n.startswith("frcopilot-") or n.startswith("FRCopilot")):
                continue
            acts = set()
            for pn in iam.list_role_policies(RoleName=n)["PolicyNames"]:
                doc = iam.get_role_policy(RoleName=n, PolicyName=pn)["PolicyDocument"]
                for st in doc.get("Statement", []):
                    a = st.get("Action", [])
                    acts.update(a if isinstance(a, list) else [a])
            managed = [p["PolicyName"] for p in iam.list_attached_role_policies(RoleName=n)["AttachedPolicies"]]
            principal = json.dumps(r["AssumeRolePolicyDocument"]["Statement"][0].get("Principal", {}))
            lines.append(f"| `{n}` | {principal} | {', '.join(sorted(acts)) or '-'} | {', '.join(managed) or '-'} |")
    return "| Role | Trusted principal | Inline-policy actions | Managed policies |\n|---|---|---|---|\n" + "\n".join(sorted(lines))


def runtime_section():
    ctl = boto3.client("bedrock-agentcore-control", region_name=REGION)
    lines = ["| Runtime | Id | Protocol | Status | Code | Env vars (names=values, no secrets) |", "|---|---|---|---|---|---|"]
    for page in ctl.get_paginator("list_agent_runtimes").paginate():
        for r in page["agentRuntimes"]:
            if r["agentRuntimeName"] not in ("frcopilot_agent", "client_profile_mcp", "planning_calc_mcp"):
                continue
            d = ctl.get_agent_runtime(agentRuntimeId=r["agentRuntimeId"])
            env = {k: v for k, v in (d.get("environmentVariables") or {}).items() if not k.startswith("OTEL_")}
            code = d.get("agentRuntimeArtifact", {}).get("codeConfiguration", {})
            lines.append(f"| {r['agentRuntimeName']} | `{r['agentRuntimeId']}` | {d.get('protocolConfiguration', {}).get('serverProtocol')} | "
                         f"{d['status']} | {code.get('runtime')} `{' '.join(code.get('entryPoint', []))}` | "
                         + "<br>".join(f"{k}={v}" for k, v in env.items()) + " |")
    return "\n".join(lines)


def cognito_section(pool_id):
    idp = boto3.client("cognito-idp", region_name=REGION)
    p = idp.describe_user_pool(UserPoolId=pool_id)["UserPool"]
    pol = p["Policies"]["PasswordPolicy"]
    clients = []
    for page in idp.get_paginator("list_user_pool_clients").paginate(UserPoolId=pool_id, MaxResults=60):
        clients += [c["ClientName"] for c in page["UserPoolClients"]]
    users = p.get("EstimatedNumberOfUsers")
    return "\n".join([
        f"- User pool `{p['Name']}` (`{pool_id}`), self sign-up: **{not p['AdminCreateUserConfig']['AllowAdminCreateUserOnly']}**, "
        f"PreSignUp trigger: `{(p.get('LambdaConfig') or {}).get('PreSignUp', 'none').split(':')[-1]}`",
        f"- Password policy: min length {pol['MinimumLength']}, uppercase {pol['RequireUppercase']}, lowercase {pol['RequireLowercase']}, "
        f"numbers {pol['RequireNumbers']}, symbols {pol['RequireSymbols']}",
        f"- Auto-verified attributes: {p.get('AutoVerifiedAttributes') or 'none'} (no confirmation emails)",
        f"- App clients: {', '.join(sorted(clients))}",
        f"- Estimated users right now: {users}",
    ])


def invocation_logging_section():
    cfg = boto3.client("bedrock", region_name=REGION).get_model_invocation_logging_configuration().get("loggingConfig")
    if not cfg:
        return "- Model-invocation logging: **off**"
    cwc = cfg.get("cloudWatchConfig", {})
    return (f"- Model-invocation logging: **on**. Text payloads go to CloudWatch `{cwc.get('logGroupName')}`; payloads over 100 KB go to "
            f"`s3://{cwc.get('largeDataDeliveryS3Config', {}).get('bucketName')}/{cwc.get('largeDataDeliveryS3Config', {}).get('keyPrefix')}`; "
            f"text={cfg.get('textDataDeliveryEnabled')}, images={cfg.get('imageDataDeliveryEnabled')}, embeddings={cfg.get('embeddingDataDeliveryEnabled')}")


def routing_section(o):
    sys.path.insert(0, str(ROOT / "lambda/llm_selector"))
    from ranking import rank
    ac = boto3.client("appconfig", region_name=REGION)
    items = ac.list_hosted_configuration_versions(ApplicationId=o["AppConfigAppId"], ConfigurationProfileId=o["AppConfigProfileId"], MaxResults=1).get("Items", [])
    if not items:
        return "- (LLM routing not seeded yet)"
    num = items[0]["VersionNumber"]
    doc = json.loads(ac.get_hosted_configuration_version(ApplicationId=o["AppConfigAppId"], ConfigurationProfileId=o["AppConfigProfileId"], VersionNumber=num)["Content"].read())
    rows = rank(doc["candidates"], doc["weights"], doc["active_model_id"], doc.get("eligibility_min_task_score", 80))
    w = doc["weights"]
    lines = [f"- Snapshot at publish time (AppConfig version {num}). **The live state changes whenever a student clicks Evaluate & apply: use `get_llm_routing` for the current values.**",
             f"- Weights: quality {w['quality']}%, cost {w['cost']}%, latency {w['latency']}%. Active model: **{next(c['name'] for c in doc['candidates'] if c['id'] == doc['active_model_id'])}** (`{doc['active_model_id']}`)",
             "", "| # | Model | Vendor | Score | Quality (task / general) | Cost per question | Price in/out per 1M | Latency | Eligible |", "|---|---|---|---|---|---|---|---|---|"]
    lines += [f"| {r['rank']} | {r['name']}{' (live)' if r['active'] else ''} | {r['vendor']} | {r['score']} | {r['task_score']} / {r['general_quality']} | ${r['cost_per_question_usd']:.4f} | "
              f"${r['price_in']} / ${r['price_out']} | {r['latency_s']} s | {'yes' if r['eligible'] else 'no'} |" for r in rows]
    lines += ["", "Model ids: " + ", ".join(f"`{c['id']}`" for c in doc["candidates"])]
    return "\n".join(lines)


def data_section():
    clients = json.loads((ROOT / "data/clients.json").read_text())
    ages = [c["age"] for c in clients]
    states = Counter(c["state"] for c in clients)
    pols = Counter(p["type"] for c in clients for p in c.get("policies", []))
    accts = Counter(a["type"] for c in clients for a in c.get("accounts", []))
    reps = Counter(c["assigned_rep"] for c in clients)
    return "\n".join([
        f"- {len(clients)} synthetic clients, ages {min(ages)}-{max(ages)}, states: " + ", ".join(f"{k} {v}" for k, v in sorted(states.items())),
        "- Policy types: " + ", ".join(f"{k} ×{v}" for k, v in pols.most_common()),
        "- Account types: " + ", ".join(f"{k} ×{v}" for k, v in accts.most_common()),
        "- Assigned reps: " + ", ".join(f"{k} ×{v}" for k, v in reps.items()),
        "- Client record fields: " + ", ".join(f"`{k}`" for k in sorted(clients[0].keys())),
    ])


REDACT = [
    (re.compile(r"eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "[REDACTED_JWT]"),
    (re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"), "[REDACTED_ACCESS_KEY_ID]"),
]


def build():
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    main, ins = outputs("FRCopilot"), outputs("FRCopilotInsights")
    inv = {s: resources(s) for s in STACKS}
    all_types = Counter(r["ResourceType"] for s in STACKS for r in inv[s])
    services = Counter(t.split("::")[1] for t in all_types.elements())
    parts = [(ROOT / "knowledge/narrative.md").read_text().rstrip(), "", f"# Live deployment facts (generated {now})", ""]

    parts += ["## Resource inventory (CloudFormation)",
              f"- **{sum(len(v) for v in inv.values())} resources** across 2 application stacks, **{len(all_types)} distinct AWS resource types**, "
              f"from **{len(services)} AWS services**: " + ", ".join(f"{k} ({v})" for k, v in services.most_common()) + ".",
              "- Also deployed but not counted above: the CDK bootstrap stack `FRCopilot-CDKToolkit` (S3 asset bucket, ECR repo, IAM roles, "
              "SSM parameter); Cognito users created at runtime; AgentCore workload identities that the service creates for each runtime and gateway; "
              "the Bedrock model-invocation logging setting.", ""]
    for s in STACKS:
        c = Counter(r["ResourceType"] for r in inv[s])
        parts += [f"### Stack `{s}`: {len(inv[s])} resources, {len(c)} types", "| Resource type | Count |", "|---|---|"]
        parts += [f"| {t} | {n} |" for t, n in sorted(c.items(), key=lambda x: (-x[1], x[0]))]
        parts += [""]
    parts += ["### Key resource names", "| Resource | Name / id |", "|---|---|"]
    for s, o in (("FRCopilot", main), ("FRCopilotInsights", ins)):
        parts += [f"| {s}: {k} | `{v}` |" for k, v in o.items()]
    parts += [""]

    parts += ["## AgentCore runtimes (live)", runtime_section(), ""]
    parts += ["## MCP tool catalog (parsed from source)"]
    for server, p in (("client-profile-mcp", "services/client_profile_mcp/main.py"), ("planning-calc-mcp", "services/planning_calc_mcp/main.py")):
        parts += [f"### {server}"] + [f"- `{n}({sig})`: {doc}" for n, sig, doc in mcp_tools(ROOT / p)]
    parts += ["- Through the Gateway the agent sees these names prefixed as `<target>___<tool>`.", ""]

    parts += ["## LLMs in use",
              f"- FR Copilot agent: `{main.get('AgentRuntimeArn', '').split('/')[-1]}` runtime calls the model that is **active in AppConfig LLM routing** "
              f"(starting model {main.get('AgentModelId')}, Claude Sonnet 4.6) via Bedrock ConverseStream, guardrail on every call; Claude models use extended thinking.",
              f"- Insights chatbot: **{ins.get('ChatbotModelId')}** (Claude Haiku 4.5) via Bedrock ConverseStream, extended thinking budget 2,048 tokens, interleaved thinking.",
              "- AgentCore Memory long-term extraction uses an AWS-managed model inside the service (not in this account's invocation logs).",
              invocation_logging_section(), ""]
    parts += ["## LLM routing (AppConfig, live snapshot)", routing_section(main), ""]
    parts += ["## FR Copilot agent system prompt (verbatim; `{today}` is filled in at run time)", "```text", agent_system_prompt().strip(), "```", ""]
    parts += ["## Insights chatbot system prompt (verbatim; the knowledge file is appended after it)", "```text", insights_system_prompt().strip(), "```", ""]
    parts += ["## Guardrail (live)", guardrail_section(main["GuardrailId"]), ""]
    parts += ["## Cognito (live)", cognito_section(main["UserPoolId"]), ""]
    parts += ["## IAM roles (live, inline-policy actions only; resources are scoped as described in section 7)", iam_section(), ""]
    parts += ["## Synthetic data summary (from data/clients.json)", data_section(), ""]
    parts += ["## Log groups the Insights chatbot can query",
              "- `/aws/bedrock-agentcore/runtimes/frcopilot_agent-*`, `client_profile_mcp-*` and `planning_calc_mcp-*` (runtime logs)",
              "- `/aws/lambda/frcopilot-chat-proxy`, `frcopilot-clients-api`, `frcopilot-presignup`, `frcopilot-insights-chat`, `frcopilot-insights-knowledge`",
              "- `/aws/vendedlogs/bedrock-agentcore/frcopilot-gateway` and `frcopilot-memory`",
              f"- `{ins.get('InvocationLogGroup')}` (Bedrock model-invocation log: prompts, reasoning, responses)",
              "- `aws/spans` (OpenTelemetry spans / Transaction Search)", ""]
    parts += [(ROOT / "knowledge/changelog.md").read_text().rstrip(), ""]
    md = "\n".join(parts)
    for rx, rep in REDACT:
        md = rx.sub(rep, md)
    return md, ins["KnowledgeBucket"]


if __name__ == "__main__":
    md, bucket = build()
    (ROOT / "knowledge/projectknowledge.md").write_text(md)
    print(f"built knowledge/projectknowledge.md ({len(md):,} chars)")
    if "--dry-run" not in sys.argv:
        r = boto3.client("s3", region_name=REGION).put_object(
            Bucket=bucket, Key="projectknowledge.md", Body=md.encode(), ContentType="text/markdown; charset=utf-8",
            Metadata={"generated-by": "scripts/publish_knowledge.py"})
        print(f"uploaded s3://{bucket}/projectknowledge.md version {r.get('VersionId')}")
