"""GET /api/knowledge: returns projectknowledge.md from the knowledge bucket plus its recent version history."""
import json
import os

import boto3
from botocore.config import Config

BUCKET = os.environ["KNOWLEDGE_BUCKET"]
KEY = os.environ["KNOWLEDGE_KEY"]
s3 = boto3.client("s3", config=Config(retries={"max_attempts": 4, "mode": "standard"}))


def _resp(code, body):
    return {"statusCode": code, "headers": {"Content-Type": "application/json", "Cache-Control": "no-store"},
            "body": json.dumps(body, default=str)}


def handler(event, _context):
    user = (event.get("requestContext", {}).get("authorizer", {}).get("claims", {}) or {}).get("username", "?")
    try:
        obj = s3.get_object(Bucket=BUCKET, Key=KEY)
        versions = s3.list_object_versions(Bucket=BUCKET, Prefix=KEY, MaxKeys=20).get("Versions", [])
    except s3.exceptions.NoSuchKey:
        return _resp(404, {"error": "projectknowledge.md has not been published yet."})
    md = obj["Body"].read().decode("utf-8")
    print(json.dumps({"level": "INFO", "service": "frcopilot-insights-knowledge", "msg": "knowledge.get",
                      "user": user, "bytes": len(md), "version": obj.get("VersionId")}))
    return _resp(200, {
        "bucket": BUCKET, "key": KEY, "markdown": md, "bytes": len(md),
        "last_modified": obj["LastModified"].isoformat(), "version_id": obj.get("VersionId"),
        "history": [{"version_id": v["VersionId"], "last_modified": v["LastModified"].isoformat(), "bytes": v["Size"]}
                    for v in sorted(versions, key=lambda v: v["LastModified"], reverse=True)],
    })
