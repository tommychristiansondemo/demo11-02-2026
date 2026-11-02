#!/usr/bin/env bash
# One-command teardown: both stacks (FRCopilotInsights, FRCopilot), leftover residue (ACM validation CNAME, runtime/CR log groups, secrets, Identity
# vault secrets, workload identities), then the FRCopilot-CDKToolkit bootstrap stack and its retained bucket/ECR repo.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
[ -d .venv ] && . .venv/bin/activate
# AppConfig hosted versions are created outside CloudFormation (by students' changes); remove them so the profile can be deleted.
python3 scripts/teardown_residue.py --pre
# Insights first (it imports the shared Cognito user pool from FRCopilot); its delete also turns Bedrock invocation logging off.
for s in FRCopilotInsights FRCopilot; do
  if aws cloudformation describe-stacks --stack-name "$s" >/dev/null 2>&1; then
    (cd infra && npx cdk destroy "$s" --force --toolkit-stack-name FRCopilot-CDKToolkit)
  fi
done
python3 scripts/teardown_residue.py
python3 scripts/teardown_residue.py --verify
