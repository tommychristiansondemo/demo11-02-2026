# FR Copilot: Amazon Bedrock AgentCore class demo

A chat assistant for financial representatives. The rep picks a **synthetic** client and asks plain-English questions
about retirement readiness and life-insurance coverage gaps. All client data, names and carriers are fictional.

```
Browser ──► CloudFront (www.awsteach.com, ACM, OAC) ──► S3 (SPA)
                     └── /api/* ──► API Gateway (Cognito authorizer, response streaming) ──► Lambda proxy
                                                                                         │ forwards rep JWT
                                                                                         ▼
                      AgentCore Runtime: frcopilot_agent (Strands + Claude Sonnet 4.6, Guardrail, Memory)
                                         │ M2M token via AgentCore Identity (Cognito client credentials)
                                         ▼
                      AgentCore Gateway (CUSTOM_JWT) ── OAuth M2M ──► Runtime MCP: client_profile_mcp ─► DynamoDB
                                                                └──► Runtime MCP: planning_calc_mcp (pure Python)
```

## Insights app (separate stack `FRCopilotInsights`, https://insights.awsteach.com)
A companion "under the hood" app for students. Its static files are in S3 behind its own CloudFront distribution, and all dynamic content comes from Lambda:
* `frcopilot-insights-chat` (Node 22, streaming) is a chatbot on **Claude Haiku 4.5** with extended thinking. It loads
  `projectknowledge.md` from the versioned bucket `frcopilot-knowledge-<acct>` and has these read-only tools:
  * `query_logs`: Logs Insights over the allowlisted FR Copilot log groups.
  * `get_llm_invocations`: reads the Bedrock model-invocation logs and returns the prompt, **reasoning** and response for
    each LLM call, for the agent and the chatbot.
  * `get_deployed_resources`: the CloudFormation inventory.
  * `get_bedrock_usage`: CloudWatch metrics.
* `frcopilot-insights-knowledge` (Python) serves the knowledge file and its version history.
* **Bedrock model-invocation logging** (account- and region-wide) is enabled by the stack and removed when the stack is deleted.
  Logs go to `/aws/bedrock/frcopilot-model-invocations`; payloads over 100 KB go to S3.
* **No secrets reach the browser or the model.** Tool output is redacted (JWTs, bearer tokens, keys, passwords), and
  the knowledge file holds names and ARNs only.
* **The knowledge file is regenerated on every deploy** by `scripts/publish_knowledge.py`. It combines the
  hand-written narrative in `knowledge/narrative.md` and `knowledge/changelog.md` with live facts from AWS and from
  the source code.

## Student IDs
Self sign-up is on. Students can create unlimited IDs, the password minimum is 6 characters, and nothing is sent by
email: a PreSignUp Lambda auto-confirms each ID. One ID works in both apps.

## Layout
| Path | What |
|---|---|
| `services/client_profile_mcp` | MCP server: `list_clients`, `get_client_profile`, `get_policies`, `get_accounts` (DynamoDB, read-only) |
| `services/planning_calc_mcp` | MCP server: `retirement_projection`, `life_insurance_needs` (DIME / income replacement), `required_minimum_distribution`, `savings_gap` (deterministic, unit-tested) |
| `services/agent` | Strands agent on AgentCore Runtime (Gateway tools, Memory STM+LTM, Guardrail, SSE events) |
| `lambda/chat_proxy` | Node 22 streaming proxy: API Gateway to Runtime, retries before first byte |
| `lambda/clients_api` | Client-picker list |
| `web/` | Plain-JS SPA (Cognito sign-in, client picker, streaming chat, MCP tool panel) |
| `infra/` | CDK (TypeScript) single stack `FRCopilot`, own bootstrap `FRCopilot-CDKToolkit` (qualifier `frcopilot`) |
| `lambda/presignup` | Cognito PreSignUp trigger (auto-confirm) |
| `lambda/insights_chat`, `lambda/insights_knowledge`, `insights_web/` | Insights app |
| `knowledge/` | Narrative + changelog sources of `projectknowledge.md` |
| `data/` | Synthetic data generator (seeded) + `clients.json` (25 clients; C-007 is the hand-built showcase) |

## Commands
```bash
./scripts/deploy.sh                 # tests, build ARM64 runtime zips, bootstrap, deploy, seed data, create demo users
python scripts/e2e_test.py          # end-to-end test through https://www.awsteach.com
python scripts/publish_knowledge.py # rebuild + upload projectknowledge.md after any change
./scripts/teardown.sh               # destroy everything incl. bootstrap + residue, then verify clean
```
Demo logins: `rep.jordan` and `rep.casey`. Passwords are generated at deploy time and stored in Secrets Manager:
`aws secretsmanager get-secret-value --secret-id frcopilot/demo-user/rep.jordan --query SecretString --output text`

## Why API Gateway + Lambda instead of calling the Runtime directly from the browser
* **Same origin.** The browser calls `/api/*` on CloudFront, so there is no CORS and the runtime ARN never reaches the browser.
* **Two auth checks.** API Gateway's Cognito authorizer rejects bad tokens at the edge, and the Lambda forwards the *same*
  JWT so AgentCore Runtime's inbound JWT authorizer validates it again. No AWS credentials are involved in the agent call.
* **Operability.** One place for input validation, throttling (stage limits), structured logs, X-Ray, and retries.
  API Gateway REST response streaming keeps token-by-token streaming end to end.

## Identity and auth
| Hop | Mechanism |
|---|---|
| Rep → API / Agent runtime | Cognito user pool access token (web client), validated by API GW and by Runtime `customJwtAuthorizer` (allowedClients = web client) |
| Agent → Gateway | AgentCore Identity OAuth2 credential provider `frcopilot-agent-gateway-m2m` (client credentials, scope `frcopilot/gateway.invoke`); Gateway allows only that client |
| Gateway → MCP runtimes | Credential provider `frcopilot-gateway-mcp-m2m` (scope `frcopilot/mcp.invoke`); MCP runtimes allow only that client |
| Secrets | Cognito M2M client secrets in Secrets Manager (`frcopilot/*`), resolved by CloudFormation dynamic references; the Identity token vault holds its own copy |

IAM: one role per runtime. The profile MCP gets read-only access to one table. The calc MCP gets only observability permissions. The agent gets Claude on one inference profile, ApplyGuardrail on one guardrail, data-plane access to one memory, and token-vault access to one provider.

## Observability
* CloudWatch → **GenAI Observability → Bedrock AgentCore**: agents, sessions, traces (Strands spans via ADOT to Transaction Search).
* Gateway and Memory send vended **application logs** (`/aws/vendedlogs/bedrock-agentcore/frcopilot-*`) and **traces** to X-Ray.
* Dashboard **FRCopilot-Operations**: runtime invocations/latency/errors, Claude tokens/latency, guardrail interventions, Lambda/API/DynamoDB.
* All services write JSON-lines structured logs.

## Cost note (approximate us-east-1 list prices; check the AWS pricing pages)
* **Idle: about $5-10/month.** That covers the dashboard (~$3), Secrets Manager (~6 secrets × $0.40), the Cognito M2M app clients, and CloudWatch storage.
  Runtimes, Gateway, Lambda, API Gateway and DynamoDB on-demand cost nothing when idle.
* **Per question: about $0.05-0.15.** Most of that is Claude Sonnet 4.6 tokens (agent loop over ~6-8 tool calls). The rest is Guardrail text units,
  AgentCore Runtime active-CPU seconds, Gateway tool calls, Memory events and retrievals, all fractions of a cent each.
* **A class of 30 students asking about 10 questions each: roughly $20-40.** Tear down after class.
