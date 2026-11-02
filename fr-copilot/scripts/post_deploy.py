"""Seed the 25 synthetic clients into DynamoDB and create the 2 demo reps from their Secrets Manager secrets (idempotent)."""
import json
from decimal import Decimal
from pathlib import Path

import boto3

REGION = "us-east-1"
ROOT = Path(__file__).resolve().parents[1]


def outputs() -> dict:
    cf = boto3.client("cloudformation", region_name=REGION)
    return {o["OutputKey"]: o["OutputValue"] for o in cf.describe_stacks(StackName="FRCopilot")["Stacks"][0]["Outputs"]}


def seed(table_name: str) -> None:
    clients = json.loads((ROOT / "data" / "clients.json").read_text(), parse_float=Decimal)
    table = boto3.resource("dynamodb", region_name=REGION).Table(table_name)
    with table.batch_writer(overwrite_by_pkeys=["client_id"]) as bw:
        for c in clients:
            bw.put_item(Item={k: v for k, v in c.items() if v is not None})
    print(f"seeded {len(clients)} synthetic clients into {table_name}")


def users(pool_id: str) -> None:
    sm = boto3.client("secretsmanager", region_name=REGION)
    idp = boto3.client("cognito-idp", region_name=REGION)
    for name in ("rep.jordan", "rep.casey"):
        cred = json.loads(sm.get_secret_value(SecretId=f"frcopilot/demo-user/{name}")["SecretString"])
        try:
            idp.admin_create_user(UserPoolId=pool_id, Username=cred["username"], MessageAction="SUPPRESS")
        except idp.exceptions.UsernameExistsException:
            pass
        idp.admin_set_user_password(UserPoolId=pool_id, Username=cred["username"], Password=cred["password"], Permanent=True)
        print(f"demo user ready: {cred['username']} (password in Secrets Manager frcopilot/demo-user/{name})")


if __name__ == "__main__":
    o = outputs()
    seed(o["ClientsTable"])
    users(o["UserPoolId"])
