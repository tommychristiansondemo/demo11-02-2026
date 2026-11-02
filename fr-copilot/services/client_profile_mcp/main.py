"""client-profile-mcp: read-only access to SYNTHETIC FR Copilot client records (DynamoDB).

Runs on AgentCore Runtime with the MCP protocol: stateless streamable HTTP on 0.0.0.0:8000/mcp.
"""
import datetime as dt
import os
import re
import time
from decimal import Decimal

import boto3
from botocore.config import Config
from mcp.server.fastmcp import FastMCP

from jsonlog import get_logger, log_event

TABLE = os.environ["CLIENTS_TABLE"]
log = get_logger("client-profile-mcp")
ddb = boto3.resource("dynamodb", config=Config(retries={"max_attempts": 5, "mode": "adaptive"},
                                               connect_timeout=3, read_timeout=5))
table = ddb.Table(TABLE)
ID_RE = re.compile(r"^C-\d{3}$")
_cache: dict = {"at": 0.0, "items": []}

mcp = FastMCP("client-profile-mcp", host="0.0.0.0", stateless_http=True,
              instructions="Read-only lookups of synthetic client profiles, insurance policies and accounts.")


def _plain(v):
    if isinstance(v, Decimal):
        return int(v) if v == v.to_integral_value() else float(v)
    if isinstance(v, dict):
        return {k: _plain(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_plain(x) for x in v]
    return v


def _normalize_id(client_id: str) -> str:
    cid = client_id.strip().upper()
    m = re.fullmatch(r"C-?(\d{1,3})", cid)
    if m:
        cid = f"C-{int(m.group(1)):03d}"
    if not ID_RE.match(cid):
        raise ValueError(f"Invalid client_id '{client_id}'. Expected format C-###, e.g. C-007.")
    return cid


def _get(client_id: str) -> dict:
    cid = _normalize_id(client_id)
    t0 = time.perf_counter()
    item = table.get_item(Key={"client_id": cid}).get("Item")
    log_event(log, "ddb.get_item", client_id=cid, found=bool(item), ms=round((time.perf_counter() - t0) * 1000, 1))
    if not item:
        raise ValueError(f"No client found with id {cid}. Use list_clients to see valid ids.")
    return _plain(item)


@mcp.tool()
def list_clients(search: str = "") -> dict:
    """List synthetic clients (id, name, age, occupation, state, assigned rep). Optional case-insensitive name/occupation filter."""
    if time.time() - _cache["at"] > 60:
        items, kwargs = [], {"ProjectionExpression": "client_id, first_name, last_name, age, occupation, #s, assigned_rep",
                             "ExpressionAttributeNames": {"#s": "state"}}
        while True:
            page = table.scan(**kwargs)
            items += page.get("Items", [])
            if "LastEvaluatedKey" not in page:
                break
            kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
        _cache.update(at=time.time(), items=sorted((_plain(i) for i in items), key=lambda c: c["client_id"]))
    s = search.strip().lower()
    rows = [c for c in _cache["items"]
            if not s or s in f"{c['first_name']} {c['last_name']} {c['occupation']}".lower()]
    log_event(log, "tool.list_clients", search=s, count=len(rows))
    return {"count": len(rows), "clients": rows, "data_notice": "All client data is synthetic."}


@mcp.tool()
def get_client_profile(client_id: str) -> dict:
    """Demographics, income, dependents, debts, retirement goals and Social Security estimate for one client (e.g. C-007)."""
    c = _get(client_id)
    log_event(log, "tool.get_client_profile", client_id=c["client_id"])
    return {k: v for k, v in c.items() if k not in ("policies", "accounts")}


@mcp.tool()
def get_policies(client_id: str) -> dict:
    """In-force insurance policies for a client: type, death benefit, premium, term, issue year, portability."""
    c = _get(client_id)
    pols = c.get("policies", [])
    year = dt.date.today().year
    kids = [d["age"] for d in c.get("dependents", []) if d.get("relationship") == "child"]
    for p in pols:  # deterministic dates so the model never does calendar math
        if p.get("term_years") and p.get("issue_year"):
            exp = p["issue_year"] + p["term_years"]
            p["expiry_year"] = exp
            p["years_remaining"] = max(exp - year, 0)
            p["client_age_at_expiry"] = c["age"] + exp - year
            if kids:
                p["youngest_child_age_at_expiry"] = min(kids) + exp - year
    total_life = sum(p.get("death_benefit", 0) for p in pols if "Life" in p.get("type", ""))
    log_event(log, "tool.get_policies", client_id=c["client_id"], count=len(pols))
    portable = sum(p.get("death_benefit", 0) for p in pols if "Life" in p.get("type", "") and p.get("portable", True))
    return {"client_id": c["client_id"], "policies": pols, "total_life_death_benefit": total_life,
            "portable_life_death_benefit": portable}


@mcp.tool()
def get_accounts(client_id: str) -> dict:
    """Retirement and investment accounts (401(k), IRA, brokerage, 529) with balances, contributions and employer match."""
    c = _get(client_id)
    accts = c.get("accounts", [])
    retirement = [a for a in accts if a["type"] in ("401(k)", "Roth IRA", "Traditional IRA", "Brokerage")]
    log_event(log, "tool.get_accounts", client_id=c["client_id"], count=len(accts))
    return {
        "client_id": c["client_id"], "accounts": accts,
        "retirement_savings_total": sum(a["balance"] for a in retirement),
        "annual_retirement_contributions_total": sum(a.get("annual_contribution", 0) + a.get("employer_match_annual", 0)
                                                     for a in retirement),
        "note": "529 balances are earmarked for education and excluded from retirement totals.",
    }


if __name__ == "__main__":
    log_event(log, "startup", table=TABLE)
    mcp.run(transport="streamable-http")
