"""Create (or refresh) the AppConfig LLM-routing document from the catalog + data/llm_benchmark.json.

First run: weights 50/25/25 (quality/cost/latency), active model = the stack's starting model (Claude Sonnet 4.6).
With --refresh: keep the current weights/active model/last change and replace only the candidate metrics.
With --reset: back to the starting state (50/25/25, starting model active, no last change), keeping the metrics.
"""
import json
import sys
import time
from pathlib import Path

import boto3

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lambda/llm_selector"))
from ranking import CATALOG, DEFAULT_WEIGHTS, ELIGIBILITY_MIN_TASK_SCORE, SWITCH_MARGIN  # noqa: E402

REGION = "us-east-1"


def main():
    o = {x["OutputKey"]: x["OutputValue"] for x in boto3.client("cloudformation", region_name=REGION).describe_stacks(StackName="FRCopilot")["Stacks"][0]["Outputs"]}
    app, env, prof, strat = o["AppConfigAppId"], o["AppConfigEnvId"], o["AppConfigProfileId"], o["AppConfigStrategyId"]
    ac = boto3.client("appconfig", region_name=REGION)
    bench = json.loads((ROOT / "data/llm_benchmark.json").read_text())
    by_id = {r["id"]: r for r in bench["results"]}
    candidates = []
    for c in CATALOG:
        b = by_id[c["id"]]
        candidates.append({**c, **{k: b[k] for k in ("task_score", "latency_s", "avg_input_tokens", "avg_output_tokens", "cost_per_question_usd")}})
    items = ac.list_hosted_configuration_versions(ApplicationId=app, ConfigurationProfileId=prof, MaxResults=1).get("Items", [])
    reset = "--reset" in sys.argv
    if items and "--refresh" not in sys.argv and not reset:
        print(f"LLM routing config already exists (version {items[0]['VersionNumber']}); use --refresh to update benchmark metrics")
        return
    doc = {"schema": 1, "weights": DEFAULT_WEIGHTS, "active_model_id": o["AgentModelId"],
           "eligibility_min_task_score": ELIGIBILITY_MIN_TASK_SCORE, "switch_margin": SWITCH_MARGIN, "last_change": None}
    latest = None
    if items:
        latest = items[0]["VersionNumber"]
    if items and not reset:
        cur = json.loads(ac.get_hosted_configuration_version(ApplicationId=app, ConfigurationProfileId=prof, VersionNumber=latest)["Content"].read())
        doc.update({k: cur[k] for k in ("weights", "active_model_id", "last_change") if k in cur})
    doc["candidates"] = candidates
    doc["benchmark"] = {"at": bench["benchmarked_at"], "question": bench["question"], "runs_per_model": bench["runs_per_model"], "rubric": bench["rubric"]}
    kw = {"LatestVersionNumber": latest} if latest else {}
    v = ac.create_hosted_configuration_version(ApplicationId=app, ConfigurationProfileId=prof, Content=json.dumps(doc).encode(),
                                               ContentType="application/json", Description="seed/refresh from scripts/seed_llm_config.py", **kw)
    for attempt in range(15):  # one deployment per environment at a time
        try:
            d = ac.start_deployment(ApplicationId=app, EnvironmentId=env, ConfigurationProfileId=prof, DeploymentStrategyId=strat,
                                    ConfigurationVersion=str(v["VersionNumber"]), Description="seed/reset LLM routing configuration")
            break
        except (ac.exceptions.ConflictException, ac.exceptions.BadRequestException):
            if attempt == 14:
                raise
            time.sleep(2)
    print(f"deployed LLM routing config version {v['VersionNumber']} (deployment {d['DeploymentNumber']}), active={doc['active_model_id']}, weights={doc['weights']}")


if __name__ == "__main__":
    main()
