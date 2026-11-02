"""Remove (or, with --verify, report) anything FR Copilot leaves behind after `cdk destroy`.

Only touches resources whose names are unambiguously FR Copilot's. In Route 53 it deletes ONLY the ACM
validation CNAME(s) under the www/insights hosts (name _<hex>.<host>.awsteach.com, value *.acm-validations.aws).
"""
import re
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = "us-east-1"
ZONE_ID = "Z02882605B7GUS0L3VHX"
HOSTS = ("www.awsteach.com.", "insights.awsteach.com.")
BOOTSTRAP = "FRCopilot-CDKToolkit"
RUNTIME_PREFIXES = ("frcopilot_agent", "client_profile_mcp", "planning_calc_mcp")
VERIFY = "--verify" in sys.argv
PRE = "--pre" in sys.argv
leftovers = []


def act(desc, fn):
    if VERIFY:
        leftovers.append(desc)
        return
    try:
        fn()
        print(f"deleted: {desc}")
    except ClientError as e:
        print(f"skip ({e.response['Error']['Code']}): {desc}")


def acm_cnames():
    r53 = boto3.client("route53")
    for page in r53.get_paginator("list_resource_record_sets").paginate(HostedZoneId=ZONE_ID):
        for rr in page["ResourceRecordSets"]:
            vals = [v["Value"] for v in rr.get("ResourceRecords", [])]
            if (rr["Type"] == "CNAME" and any(re.fullmatch(r"_[0-9a-f]{32}\." + re.escape(h), rr["Name"]) for h in HOSTS)
                    and vals and all(v.rstrip(".").endswith("acm-validations.aws") for v in vals)):
                act(f"Route53 ACM validation CNAME {rr['Name']}", lambda rr=rr: r53.change_resource_record_sets(
                    HostedZoneId=ZONE_ID, ChangeBatch={"Changes": [{"Action": "DELETE", "ResourceRecordSet": rr}]}))
    for host in HOSTS:
        for rtype in ("A", "AAAA"):
            rs = r53.list_resource_record_sets(HostedZoneId=ZONE_ID, StartRecordName=host, StartRecordType=rtype, MaxItems="1")
            rr = rs["ResourceRecordSets"][0] if rs["ResourceRecordSets"] else None
            if rr and rr["Name"] == host and rr["Type"] == rtype and "AliasTarget" in rr and "cloudfront.net" in rr["AliasTarget"]["DNSName"]:
                if VERIFY:
                    leftovers.append(f"Route53 {rtype} alias {host} (stack-owned; should have been removed by cdk destroy)")


def log_groups():
    logs = boto3.client("logs", region_name=REGION)
    prefixes = ["/aws/bedrock-agentcore/runtimes/" + p for p in RUNTIME_PREFIXES] + [
        "/aws/lambda/FRCopilot", "/aws/lambda/frcopilot-", "/aws/vendedlogs/bedrock-agentcore/frcopilot", "FRCopilot",
        "/aws/bedrock/frcopilot"]
    for pre in prefixes:
        for page in logs.get_paginator("describe_log_groups").paginate(logGroupNamePrefix=pre):
            for g in page["logGroups"]:
                act(f"log group {g['logGroupName']}", lambda n=g["logGroupName"]: logs.delete_log_group(logGroupName=n))


def secrets():
    sm = boto3.client("secretsmanager", region_name=REGION)
    for page in sm.get_paginator("list_secrets").paginate(IncludePlannedDeletion=True):
        for s in page["SecretList"]:
            n = s["Name"]
            if n.startswith("frcopilot/") or n.startswith("bedrock-agentcore-identity!default/oauth2/frcopilot-"):
                act(f"secret {n}", lambda a=s["ARN"]: sm.delete_secret(SecretId=a, ForceDeleteWithoutRecovery=True))


def agentcore_identities():
    ac = boto3.client("bedrock-agentcore-control", region_name=REGION)
    try:
        for page in ac.get_paginator("list_workload_identities").paginate():
            for w in page.get("workloadIdentities", []):
                if w["name"].startswith(RUNTIME_PREFIXES) or w["name"].startswith("frcopilot"):
                    act(f"workload identity {w['name']}", lambda n=w["name"]: ac.delete_workload_identity(name=n))
    except ClientError as e:
        print(f"workload identities: {e}")
    for page in ac.get_paginator("list_oauth2_credential_providers").paginate():
        for p in page.get("credentialProviders", []):
            if p["name"].startswith("frcopilot"):
                act(f"oauth2 credential provider {p['name']}", lambda n=p["name"]: ac.delete_oauth2_credential_provider(name=n))


def invocation_logging():
    """Bedrock model-invocation logging is account-wide; remove it only if it is ours (points at our log group)."""
    br = boto3.client("bedrock", region_name=REGION)
    cfg = br.get_model_invocation_logging_configuration().get("loggingConfig") or {}
    if (cfg.get("cloudWatchConfig") or {}).get("logGroupName", "").startswith("/aws/bedrock/frcopilot"):
        act("Bedrock model-invocation logging configuration (FR Copilot)", br.delete_model_invocation_logging_configuration)


def buckets():
    s3 = boto3.resource("s3")
    for b in s3.buckets.all():
        if b.name.startswith("frcopilot-"):
            def drop(b=b):
                b.object_versions.delete()
                b.delete()
            act(f"S3 bucket {b.name}", drop)


def appconfig(pre=False):
    """Delete hosted config versions (created at run time by the LLM selector) and, after the stack, any leftover app."""
    ac = boto3.client("appconfig", region_name=REGION)
    for page in ac.get_paginator("list_applications").paginate():
        for app in page["Items"]:
            if app["Name"] != "frcopilot":
                continue
            for prof in ac.list_configuration_profiles(ApplicationId=app["Id"]).get("Items", []):
                for v in ac.list_hosted_configuration_versions(ApplicationId=app["Id"], ConfigurationProfileId=prof["Id"], MaxResults=50).get("Items", []):
                    act(f"AppConfig hosted version {prof['Name']} v{v['VersionNumber']}", lambda a=app["Id"], p=prof["Id"], n=v["VersionNumber"]:
                        ac.delete_hosted_configuration_version(ApplicationId=a, ConfigurationProfileId=p, VersionNumber=n))
            if not pre:
                act(f"AppConfig application {app['Name']}", lambda a=app["Id"]: _delete_appconfig_app(ac, a))
    for page in ac.get_paginator("list_deployment_strategies").paginate():
        for st in page["Items"]:
            if st["Name"] == "frcopilot-instant" and not pre:
                act(f"AppConfig deployment strategy {st['Name']}", lambda i=st["Id"]: ac.delete_deployment_strategy(DeploymentStrategyId=i))


def _delete_appconfig_app(ac, app_id):
    for prof in ac.list_configuration_profiles(ApplicationId=app_id).get("Items", []):
        ac.delete_configuration_profile(ApplicationId=app_id, ConfigurationProfileId=prof["Id"], DeletionProtectionCheck="BYPASS")
    for env in ac.list_environments(ApplicationId=app_id).get("Items", []):
        ac.delete_environment(ApplicationId=app_id, EnvironmentId=env["Id"], DeletionProtectionCheck="BYPASS")
    ac.delete_application(ApplicationId=app_id)


def bootstrap():
    cf = boto3.client("cloudformation", region_name=REGION)
    try:
        res = cf.describe_stack_resources(StackName=BOOTSTRAP)["StackResources"]
    except ClientError:
        return
    if VERIFY:
        leftovers.append(f"stack {BOOTSTRAP}")
        return
    s3 = boto3.resource("s3")
    for r in res:
        if r["ResourceType"] == "AWS::S3::Bucket" and r.get("PhysicalResourceId"):
            b = s3.Bucket(r["PhysicalResourceId"])
            try:
                b.object_versions.delete()
                b.delete()
                print(f"deleted: bootstrap bucket {b.name}")
            except ClientError as e:
                print(f"bucket {b.name}: {e}")
        if r["ResourceType"] == "AWS::ECR::Repository" and r.get("PhysicalResourceId"):
            try:
                boto3.client("ecr", region_name=REGION).delete_repository(repositoryName=r["PhysicalResourceId"], force=True)
                print(f"deleted: bootstrap ECR {r['PhysicalResourceId']}")
            except ClientError as e:
                print(f"ecr: {e}")
    cf.delete_stack(StackName=BOOTSTRAP)
    cf.get_waiter("stack_delete_complete").wait(StackName=BOOTSTRAP)
    print(f"deleted: stack {BOOTSTRAP}")


def stacks_and_tags():
    cf = boto3.client("cloudformation", region_name=REGION)
    try:
        cf.describe_stacks(StackName="FRCopilot")
        leftovers.append("stack FRCopilot")
    except ClientError:
        pass
    try:
        cf.describe_stacks(StackName="FRCopilotInsights")
        leftovers.append("stack FRCopilotInsights")
    except ClientError:
        pass
    tagging = boto3.client("resourcegroupstaggingapi", region_name=REGION)
    for page in tagging.get_paginator("get_resources").paginate(TagFilters=[{"Key": "Project", "Values": ["FRCopilot"]}]):
        for r in page["ResourceTagMappingList"]:
            leftovers.append(f"tagged resource {r['ResourceARN']} (tag index can lag a few minutes)")


if __name__ == "__main__":
    if PRE:
        appconfig(pre=True)
        sys.exit(0)
    acm_cnames()
    log_groups()
    secrets()
    agentcore_identities()
    invocation_logging()
    appconfig()
    buckets()
    bootstrap()
    if VERIFY:
        stacks_and_tags()
        print("\nVERIFY: clean - no FR Copilot residue found" if not leftovers else
              "\nVERIFY: leftovers:\n  " + "\n  ".join(leftovers))
        sys.exit(1 if leftovers else 0)
