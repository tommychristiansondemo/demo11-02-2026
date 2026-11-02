"""Benchmark the 8 candidate LLMs on the real FR Copilot task, so the routing scores are measured, not guessed.

For each model: run the agent's actual system prompt + the actual MCP tools (imported from services/*, executing the
real code against the real DynamoDB table) + the live Guardrail through Bedrock Converse, on the showcase question
"Is client C-007 on track to retire at 65, and do they have a coverage gap?". Each run is graded with a fixed rubric
(below). Latency = median end-to-end seconds; cost = average tokens x list price.
Output: data/llm_benchmark.json (consumed by scripts/seed_llm_config.py). Usage: python scripts/llm_benchmark.py [--runs 2]
"""
import ast
import asyncio
import concurrent.futures as cf
import datetime as dt
import importlib.util
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path

import boto3
from botocore.config import Config

ROOT = Path(__file__).resolve().parents[1]
REGION = "us-east-1"
sys.path[:0] = [str(ROOT / "services/shared"), str(ROOT / "lambda/llm_selector")]
from ranking import CATALOG  # noqa: E402

QUESTION = "Is client C-007 on track to retire at 65, and do they have a coverage gap?"
RUBRIC = [  # (points, label, check(text, tools_called))
    (10, "fetched client profile", lambda t, tc: "get_client_profile" in tc),
    (10, "fetched accounts", lambda t, tc: "get_accounts" in tc),
    (10, "fetched policies", lambda t, tc: "get_policies" in tc),
    (10, "ran retirement_projection", lambda t, tc: "retirement_projection" in tc),
    (10, "ran life_insurance_needs", lambda t, tc: "life_insurance_needs" in tc),
    (15, "verdict: not on track", lambda t, tc: re.search(r"not (currently )?on track|off track|falls? short|behind|shortfall", t, re.I) is not None),
    (10, "funded ratio ~53%", lambda t, tc: re.search(r"\b5[0-6](\.\d)?\s?%", t) is not None),
    (15, "DIME gap ~$1.43M", lambda t, tc: re.search(r"1[,.]43\d|1\.4\d?\s?(m|million)|1,433", t, re.I) is not None),
    (10, "term expiry 2036 / child age", lambda t, tc: "2036" in t),
]


def load_module(name: str, path: Path):
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    sys.path.remove(str(path.parent))
    return mod


def agent_system_prompt() -> str:
    tree = ast.parse((ROOT / "services/agent/main.py").read_text())
    for node in tree.body:
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") == "SYSTEM_PROMPT":
            s = "".join(p.value if isinstance(p, ast.Constant) else "{" + ast.unparse(p.value) + "}" for p in node.value.values)
            return s.replace("{{today}}", dt.date.today().isoformat()).replace("{today}", dt.date.today().isoformat())
    raise RuntimeError("SYSTEM_PROMPT not found")


def build_tools():
    os.environ.setdefault("CLIENTS_TABLE", "frcopilot-clients")
    os.environ.setdefault("AWS_DEFAULT_REGION", REGION)
    servers = {"client-profile-mcp": load_module("profile_main", ROOT / "services/client_profile_mcp/main.py").mcp,
               "planning-calc-mcp": load_module("calc_main", ROOT / "services/planning_calc_mcp/main.py").mcp}
    specs, impl = [], {}
    for prefix, server in servers.items():
        for t in asyncio.run(server.list_tools()):
            name = f"{prefix}___{t.name}"
            specs.append({"toolSpec": {"name": name, "description": t.description or t.name, "inputSchema": {"json": t.inputSchema}}})
            impl[name] = (server, t.name)
    return specs, impl


def call_tool(impl, name, args):
    server, tool = impl[name]
    res = asyncio.run(server.call_tool(tool, args))
    content = res[0] if isinstance(res, tuple) else res
    return "".join(getattr(c, "text", "") for c in content)


def run_once(model, specs, impl, system, guardrail):
    br = boto3.client("bedrock-runtime", region_name=REGION, config=Config(retries={"max_attempts": 8, "mode": "adaptive"}, read_timeout=180))
    messages = [{"role": "user", "content": [{"text": QUESTION}]}]
    tin = tout = 0
    tools_called, text = [], ""
    t0 = time.time()
    for _ in range(14):
        kw = dict(modelId=model["id"], system=[{"text": system}], messages=messages, toolConfig={"tools": specs},
                  inferenceConfig={"maxTokens": model["max_tokens"]},
                  guardrailConfig={"guardrailIdentifier": guardrail[0], "guardrailVersion": guardrail[1]})
        if model["request_fields"]:
            kw["additionalModelRequestFields"] = model["request_fields"]
        r = br.converse(**kw)
        tin += r["usage"]["inputTokens"]
        tout += r["usage"]["outputTokens"]
        msg = r["output"]["message"]
        messages.append(msg)
        text += "".join(b.get("text", "") for b in msg["content"])
        uses = [b["toolUse"] for b in msg["content"] if "toolUse" in b]
        if r["stopReason"] != "tool_use" or not uses:
            break
        results = []
        for u in uses:
            tools_called.append(u["name"].split("___")[-1])
            try:
                out, status = call_tool(impl, u["name"], u.get("input") or {}), "success"
            except Exception as e:  # tool errors are fed back like the Gateway would
                out, status = f"Error: {e}", "error"
            results.append({"toolResult": {"toolUseId": u["toolUseId"], "content": [{"text": out}], "status": status}})
        messages.append({"role": "user", "content": results})
    secs = time.time() - t0
    checks = [(pts, label, bool(fn(text, tools_called))) for pts, label, fn in RUBRIC]
    return {"seconds": round(secs, 2), "input_tokens": tin, "output_tokens": tout, "tools_called": tools_called,
            "score": sum(p for p, _, ok in checks if ok), "missed": [label for _, label, ok in checks if not ok],
            "answer_excerpt": text[-600:]}


def bench_model(model, runs, specs, impl, system, guardrail):
    out = []
    for _ in range(runs):
        try:
            out.append(run_once(model, specs, impl, system, guardrail))
        except Exception as e:
            out.append({"error": str(e)[:300], "score": 0, "seconds": None, "input_tokens": 0, "output_tokens": 0})
    ok = [r for r in out if r.get("seconds")]
    tin = statistics.mean(r["input_tokens"] for r in ok) if ok else 0
    tout = statistics.mean(r["output_tokens"] for r in ok) if ok else 0
    return {
        "id": model["id"], "task_score": round(statistics.mean(r["score"] for r in out)),
        "latency_s": round(statistics.median(r["seconds"] for r in ok), 1) if ok else 120.0,
        "avg_input_tokens": round(tin), "avg_output_tokens": round(tout),
        "cost_per_question_usd": round((tin * model["price_in"] + tout * model["price_out"]) / 1e6, 5) if ok else 1.0,
        "runs": out,
    }


def main():
    runs = int(sys.argv[sys.argv.index("--runs") + 1]) if "--runs" in sys.argv else 2
    o = {x["OutputKey"]: x["OutputValue"] for x in boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName="FRCopilot")["Stacks"][0]["Outputs"]}
    guardrail = (o["GuardrailId"], o["GuardrailVersion"])
    specs, impl = build_tools()
    system = agent_system_prompt() + "\n\nThe rep currently has client C-007 selected in the UI."
    print(f"{len(specs)} tools, {len(CATALOG)} models, {runs} run(s) each")
    with cf.ThreadPoolExecutor(len(CATALOG)) as ex:
        results = list(ex.map(lambda m: bench_model(m, runs, specs, impl, system, guardrail), CATALOG))
    for m, r in zip(CATALOG, results):
        missed = sorted({x for run in r["runs"] for x in run.get("missed", [])})
        errs = [run["error"][:80] for run in r["runs"] if run.get("error")]
        print(f"{m['name']:<22} task {r['task_score']:>3}/100  {r['latency_s']:>6}s  ${r['cost_per_question_usd']:.4f}/q  "
              f"tok {r['avg_input_tokens']}/{r['avg_output_tokens']}  missed={missed} {errs}")
    out = {"benchmarked_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "question": QUESTION, "runs_per_model": runs,
           "rubric": [{"points": p, "check": label} for p, label, _ in RUBRIC], "results": results}
    (ROOT / "data/llm_benchmark.json").write_text(json.dumps(out, indent=2))
    print("wrote data/llm_benchmark.json")


if __name__ == "__main__":
    main()
