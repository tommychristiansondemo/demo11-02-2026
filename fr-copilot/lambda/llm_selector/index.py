"""FR Copilot LLM routing API (AppConfig-backed).

GET  /api/llm  -> current preferences, the 8-model ranking for those preferences, active model, last change.
POST /api/llm  {"quality":50,"cost":25,"latency":25}
     -> validate (sum 100), rank, decide whether the live agent should switch models, then write a new AppConfig
        hosted configuration version and deploy it (instant strategy). The agent picks the change up within ~15 s.
"""
import json
import os
import time
from datetime import datetime, timezone

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from ranking import ELIGIBILITY_MIN_TASK_SCORE, SWITCH_MARGIN, decide, rank, validate_weights

APP, ENV, PROFILE, STRATEGY = (os.environ[k] for k in ("APPCONFIG_APP_ID", "APPCONFIG_ENV_ID", "APPCONFIG_PROFILE_ID", "APPCONFIG_STRATEGY_ID"))
cfg = Config(retries={"max_attempts": 5, "mode": "adaptive"})
ac = boto3.client("appconfig", config=cfg)


def log(level, msg, **kw):
    print(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "level": level, "service": "frcopilot-llm-selector", "msg": msg, **kw}, default=str))


def resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"}, "body": json.dumps(body, default=str)}


def current():
    """Latest hosted version (source of truth for writes) + the deployment state of the environment."""
    versions = ac.list_hosted_configuration_versions(ApplicationId=APP, ConfigurationProfileId=PROFILE, MaxResults=1).get("Items", [])
    if not versions:
        raise RuntimeError("LLM routing configuration has not been seeded yet.")
    num = versions[0]["VersionNumber"]
    doc = json.loads(ac.get_hosted_configuration_version(ApplicationId=APP, ConfigurationProfileId=PROFILE, VersionNumber=num)["Content"].read())
    deps = ac.list_deployments(ApplicationId=APP, EnvironmentId=ENV, MaxResults=1).get("Items", [])
    return num, doc, (deps[0] if deps else None)


def view(doc, num, dep):
    ranking = rank(doc["candidates"], doc["weights"], doc["active_model_id"], doc.get("eligibility_min_task_score", ELIGIBILITY_MIN_TASK_SCORE))
    active = next((c for c in doc["candidates"] if c["id"] == doc["active_model_id"]), {})
    return {
        "weights": doc["weights"], "active_model_id": doc["active_model_id"], "active_model_name": active.get("name"),
        "ranking": [{k: r.get(k) for k in ("rank", "id", "name", "vendor", "strengths", "score", "quality_score", "cost_score", "latency_score",
                                           "task_score", "general_quality", "quality_raw", "cost_per_question_usd", "latency_s", "price_in", "price_out",
                                           "eligible", "active")} for r in ranking],
        "eligibility_min_task_score": doc.get("eligibility_min_task_score", ELIGIBILITY_MIN_TASK_SCORE),
        "switch_margin": doc.get("switch_margin", SWITCH_MARGIN), "benchmark": doc.get("benchmark", {}),
        "last_change": doc.get("last_change"), "config_version": num,
        "deployment": {"number": dep["DeploymentNumber"], "state": dep["State"], "version": dep.get("ConfigurationVersion"),
                       "started": dep.get("StartedAt")} if dep else None,
    }


def handler(event, _ctx):
    method = event.get("httpMethod", "GET")
    user = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims", {}).get("username", "unknown")
    try:
        num, doc, dep = current()
        if method == "GET":
            return resp(200, view(doc, num, dep))
        try:
            weights = validate_weights(json.loads(event.get("body") or "{}"))
        except (ValueError, json.JSONDecodeError) as e:
            return resp(400, {"error": str(e)})
        ranking = rank(doc["candidates"], weights, doc["active_model_id"], doc.get("eligibility_min_task_score", ELIGIBILITY_MIN_TASK_SCORE))
        decision = decide(ranking, doc["active_model_id"], doc.get("switch_margin", SWITCH_MARGIN))
        prev = doc["active_model_id"]
        doc["weights"] = weights
        doc["active_model_id"] = decision["target"]
        doc["last_change"] = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "by": user, "weights": weights,
                              "switched": decision["switch"], "from": prev, "to": decision["target"], "reason": decision["reason"],
                              "top3": [r["name"] for r in ranking[:3]]}
        # Optimistic lock on the version number; AppConfig allows one deployment per environment at a time, so wait briefly.
        new = ac.create_hosted_configuration_version(
            ApplicationId=APP, ConfigurationProfileId=PROFILE, Content=json.dumps(doc).encode(), ContentType="application/json",
            LatestVersionNumber=num, Description=f"{user}: Q{weights['quality']}/C{weights['cost']}/L{weights['latency']} -> {decision['target']}"[:1024])
        for attempt in range(12):
            try:
                d = ac.start_deployment(ApplicationId=APP, EnvironmentId=ENV, ConfigurationProfileId=PROFILE, DeploymentStrategyId=STRATEGY,
                                        ConfigurationVersion=str(new["VersionNumber"]), Description=doc["last_change"]["reason"][:1024])
                break
            except ClientError as e:
                if e.response["Error"]["Code"] not in ("ConflictException", "BadRequestException") or attempt == 11:
                    raise
                time.sleep(1)
        log("INFO", "llm.preferences.applied", user=user, weights=weights, switched=decision["switch"], frm=prev, to=decision["target"],
            reason=decision["reason"], config_version=new["VersionNumber"], deployment=d["DeploymentNumber"])
        out = view(doc, new["VersionNumber"], {"DeploymentNumber": d["DeploymentNumber"], "State": d["State"],
                                               "ConfigurationVersion": d["ConfigurationVersion"], "StartedAt": d.get("StartedAt")})
        out["decision"] = decision
        return resp(200, out)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        log("WARN", "appconfig.error", user=user, code=code, error=str(e)[:300])
        if code in ("ConflictException", "BadRequestException") and "version" in str(e).lower():
            return resp(409, {"error": "Another student just changed the settings. Refresh and try again."})
        return resp(502, {"error": "Could not update the LLM routing configuration. Please retry."})
    except Exception as e:
        log("ERROR", "llm.selector.error", user=user, error=repr(e)[:300])
        return resp(500, {"error": str(e)[:200]})
