"""GET /api/clients - summary list of SYNTHETIC clients for the UI picker (read-only, Cognito-protected)."""
import json
import os
import time
from decimal import Decimal

import boto3
from botocore.config import Config

table = boto3.resource("dynamodb", config=Config(retries={"max_attempts": 5, "mode": "adaptive"})).Table(
    os.environ["CLIENTS_TABLE"])
FIELDS = ["client_id", "first_name", "last_name", "age", "occupation", "state", "annual_income",
          "marital_status", "dependents", "assigned_rep", "target_retirement_age"]


def _plain(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, list):
        return [_plain(x) for x in v]
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    return v


def handler(event, context):
    t0 = time.perf_counter()
    names = {f"#f{i}": f for i, f in enumerate(FIELDS)}
    kwargs = {"ProjectionExpression": ", ".join(names), "ExpressionAttributeNames": names}
    items = []
    while True:
        page = table.scan(**kwargs)
        items += page.get("Items", [])
        if "LastEvaluatedKey" not in page:
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    rows = sorted((_plain(i) for i in items), key=lambda c: c["client_id"])
    for r in rows:
        r["dependents"] = len(r.get("dependents", []))
    user = event.get("requestContext", {}).get("authorizer", {}).get("claims", {}).get("username", "unknown")
    print(json.dumps({"level": "INFO", "service": "frcopilot-clients-api", "msg": "clients.list", "user": user,
                      "count": len(rows), "ms": round((time.perf_counter() - t0) * 1000, 1)}))
    return {"statusCode": 200, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps({"clients": rows})}
