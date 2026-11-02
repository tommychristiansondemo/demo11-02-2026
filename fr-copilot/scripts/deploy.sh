#!/usr/bin/env bash
# Build runtime packages + Lambda deps, bootstrap (own toolkit stack), deploy, then seed data + demo users.
set -euo pipefail
cd "$(dirname "$0")/.."
export AWS_REGION=us-east-1 AWS_DEFAULT_REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
[ -d .venv ] || python3 -m venv .venv
. .venv/bin/activate
pip -q install boto3 pytest >/dev/null
python -m pytest -q tests
python data/generate_clients.py
python scripts/build_runtimes.py
(cd lambda/insights_chat && npm install --omit=dev --no-audit --no-fund >/dev/null)
cd infra
[ -d node_modules ] || npm ci
if ! aws cloudformation describe-stacks --stack-name FRCopilot-CDKToolkit >/dev/null 2>&1; then
  npx cdk bootstrap "aws://${ACCOUNT}/us-east-1" --toolkit-stack-name FRCopilot-CDKToolkit --qualifier frcopilot \
    --tags Project=FRCopilot --tags Owner=Instructor
fi
npx cdk deploy FRCopilot FRCopilotInsights --require-approval never --toolkit-stack-name FRCopilot-CDKToolkit --outputs-file cdk-outputs.json
cd ..
python scripts/post_deploy.py
python scripts/seed_llm_config.py      # AppConfig LLM routing document (first deploy only; --refresh after re-benchmarking)
python scripts/publish_knowledge.py   # keep projectknowledge.md in S3 in sync with what is deployed
