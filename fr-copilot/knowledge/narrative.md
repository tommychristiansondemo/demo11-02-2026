# FR Copilot: project knowledge

This file is the source of truth for the FR Copilot Insights chatbot. It is regenerated and re-uploaded to S3
(`s3://frcopilot-knowledge-<account>/projectknowledge.md`, versioned) on every deployment. The sections after
"Live deployment facts" are generated from the live AWS account and the source code at publish time.

## 1. What FR Copilot is
FR Copilot is a class demo of **Amazon Bedrock AgentCore**. It is a chat assistant for *financial representatives*
(reps) at a fictional life-insurance and financial-planning firm. A rep picks a client and asks plain-English
questions such as *"Is client C-007 on track to retire at 65, and do they have a coverage gap?"*. An AI agent
looks up the client's data, runs deterministic financial calculations, and answers with tables, assumptions and
next steps. Every answer ends with "For illustration only, not financial advice."

- Main app: **https://www.awsteach.com** (CloudFormation stack `FRCopilot`)
- Insights app (this chatbot): **https://insights.awsteach.com** (separate stack `FRCopilotInsights`)
- AWS region: **us-east-1** (single account). Infrastructure as code: **AWS CDK v2 in TypeScript**.
- Built from scratch for this class by Claude Code (Anthropic's coding agent), driven by the instructor's spec.
- Every resource is tagged `Project=FRCopilot` and `Owner=Instructor`.

## 2. Where the data comes from (ALL SYNTHETIC)
- There is **no real person or real company data** anywhere in the demo.
- The 25 clients (C-001 to C-025) are produced by `data/generate_clients.py`. It is a seeded, deterministic
  Python generator (`random.Random(20261102)`) that combines made-up first and last names, occupations with
  typical incomes, US states, dependents, debts, policies and accounts.
- Carriers are fictional: "Lakeshore Mutual Life (fictional)" and "Employer plan (fictional)". Account numbers
  are masked placeholders (`****1234`), and every record has `synthetic: true`.
- **C-007, Marisol Thistlewood**, is hand-built as the showcase client:
  - age 46, income $142k (spouse $38k), two children aged 12 and 9
  - mortgage $318k, other debt $14.5k
  - a 20-year $250k term policy issued 2016, plus $284k of non-portable group life
  - a 401(k) of $212k, Roth $38.5k, brokerage $22k and a 529 of $18.4k
  - Social Security estimate $32,400/yr
- The generator writes `data/clients.json`. `scripts/post_deploy.py` loads it into the **DynamoDB table
  `frcopilot-clients`** (on-demand, partition key `client_id`).
- Only the `client-profile-mcp` server reads that table (read-only IAM: GetItem/Scan on that one table). So does
  the small `frcopilot-clients-api` Lambda that feeds the client picker in the UI.

## 3. Architecture (request path)
```
Browser ─► CloudFront (www.awsteach.com, ACM TLS, OAC) ─► S3 (static SPA)
              └─ /api/* ─► API Gateway REST (Cognito authorizer, response streaming)
                             ├─ POST /api/chat ─► Lambda frcopilot-chat-proxy (Node 22) ── forwards the user's JWT ──┐
                             └─ GET /api/clients ─► Lambda frcopilot-clients-api (Python) ─► DynamoDB              │
                                                                                                                  ▼
  AgentCore Runtime "frcopilot_agent" (Strands Agents + Claude Sonnet 4.6 on Bedrock, Guardrail, Memory)
        │  M2M OAuth token from AgentCore Identity (Cognito client-credentials)
        ▼
  AgentCore Gateway "frcopilot-gateway" (MCP, CUSTOM_JWT inbound) ── OAuth M2M ──► AgentCore Runtime (MCP) client_profile_mcp ─► DynamoDB
                                                                              └──► AgentCore Runtime (MCP) planning_calc_mcp (pure Python math)
```
Step by step for one question:
1. The browser signs the user in with Amazon Cognito (USER_PASSWORD_AUTH) and gets an access token (JWT).
2. The SPA POSTs `{prompt, client_id, session_id}` to `/api/chat` on the same CloudFront domain.
3. API Gateway's Cognito authorizer validates the JWT. The chat-proxy Lambda then forwards the **same** JWT to the
   AgentCore Runtime invocation endpoint, with the session id header. It retries 429/5xx up to 3 times before
   the first byte.
4. AgentCore Runtime validates the JWT a second time (customJwtAuthorizer, allowed client = the web app client)
   and runs the Strands agent in an isolated microVM session.
5. The agent obtains an M2M OAuth token through **AgentCore Identity** (`@requires_access_token`, provider
   `frcopilot-agent-gateway-m2m`) and connects to the **AgentCore Gateway** over MCP (streamable HTTP).
6. The Gateway lists the tools from both MCP servers as one catalog, with names prefixed by target, such as
   `client-profile-mcp___get_policies`. It calls each MCP server runtime with its own M2M token
   (provider `frcopilot-gateway-mcp-m2m`).
7. Claude decides which tools to call (typically 6-8 for the C-007 question). All numbers come from the
   planning-calc tools, not from the LLM.
8. Text, tool start/end events and a final "done" event stream back as Server-Sent Events. The UI shows the
   answer plus an "MCP tool calls" panel colour-coded by server.
9. The conversation is saved to AgentCore Memory (short-term). Rep preferences are extracted to long-term memory.

## 4. Components
### 4.1 MCP servers (on AgentCore Runtime, protocol MCP, Python 3.12, FastMCP stateless HTTP)
- **client-profile-mcp**: `list_clients`, `get_client_profile`, `get_policies`, `get_accounts`. Reads DynamoDB.
  It adds deterministic computed fields such as policy `expiry_year`, `years_remaining`, `client_age_at_expiry`,
  `youngest_child_age_at_expiry`, `portable_life_death_benefit` and `retirement_savings_total`, so the LLM never
  does calendar math.
- **planning-calc-mcp**: `retirement_projection`, `life_insurance_needs` (DIME or income-replacement),
  `required_minimum_distribution` (IRS Uniform Lifetime Table) and `savings_gap`. Pure deterministic Python
  with unit tests (`tests/test_calc.py`) and no data store.
- Both are deployed as **direct code deployment** ARM64 zip packages (no containers). They are built by
  `scripts/build_runtimes.py` with `pip --platform manylinux2014_aarch64` and run with the entry point
  `opentelemetry-instrument main.py` for ADOT tracing.

### 4.2 The agent (AgentCore Runtime, protocol HTTP)
- Framework: **Strands Agents** with `BedrockAgentCoreApp`.
- Model: **switchable at run time** (see section 4.7 LLM routing).
  - The starting model was **Claude Sonnet 4.6** (`us.anthropic.claude-sonnet-4-6`). The Claude 5.x models
    returned AccessDenied in this account at build time.
  - The agent reads the active model from AWS AppConfig on every request, cached for 15 seconds, so students can
    move it to any of 8 models from 6 vendors without a redeploy.
  - **Use the live tool `get_llm_routing` to answer "which model is live now?"**
- **Extended thinking (reasoning)**:
  - Claude models think: adaptive mode for Sonnet and Opus 4.6, and a 2,048-token budget for Haiku 4.5. Other
    models use their default behavior.
  - Reasoning from whichever model is live is captured by Bedrock model-invocation logging and in the agent's own
    log as `llm.reasoning` events.
  - When the model changes mid-conversation, the agent drops earlier reasoning blocks from the restored history
    so the new model can continue the session.
- **Bedrock Guardrail** `frcopilot-guardrail` on every model call:
  - a denied topic for investment guarantees
  - PII handling: block SSN, card, bank account and routing numbers; anonymize email and phone
  - content filters, including prompt-attack detection
  - a blocked message that is redacted into the stream
- **AgentCore Memory** `frcopilot_memory`:
  - **short-term**: the conversation events of each session, kept for 30 days
  - **long-term**: a `userPreference` strategy in the namespace `/preferences/{actorId}`, where the actor is the
    Cognito username. For example, "I prefer income replacement and brief answers" is remembered across sessions.
- The selected client is passed in the **system prompt**, not the user turn, so the guardrail judges only what
  the user typed. An earlier version put it in the user turn, and the prompt-attack filter flagged harmless
  "remember for future chats" requests.
- The application, not the LLM, appends the disclaimer to every answer.

### 4.7 LLM routing: same app, different model (Bedrock flexibility demo)
- **Why it works:** Amazon Bedrock's **Converse API** gives one request/response, tool-use and streaming format
  across model vendors. Switching the agent's model is just a different `modelId`, with the same code, Gateway
  tools, guardrail and memory. The **Bedrock Guardrail** applies to every model.
- **Where the setting lives: AWS AppConfig.** Application `frcopilot`, environment `live`, configuration
  profile `llm-selection`:
  - It is a hosted, freeform JSON document with a JSON-Schema validator. Weights must be integers from 0 to 100,
    and the model id must be one of the 8 candidates.
  - It holds the **weights**: quality / cost / latency, starting at **50 / 25 / 25**.
  - It holds the **8-model catalog** with benchmark metrics, the **active model id**, the eligibility threshold,
    and the **last change**: who, when, the decision and the reason.
  - Changes deploy with the custom strategy `frcopilot-instant` (all at once, no bake).
  - Every change is a new **hosted configuration version** plus an **AppConfig deployment**, so the full history
    is kept and auditable.
- **UI:** the FR Copilot header shows a "Model: …" chip. Clicking it opens the **LLM routing** panel:
  - three **linked sliders** that always total 100% (moving one rebalances the other two proportionally)
  - an **Evaluate & apply** button
  - the **ranking of all 8 LLMs** for the currently configured preferences, previewed live while you drag
  Any signed-in student can change it. The setting is shared, so everyone's chat moves to the new model.
- **API:** `GET/POST /api/llm` goes through API Gateway (Cognito) to the Lambda **`frcopilot-llm-selector`**
  (Python). On POST it:
  1. validates that the weights sum to 100
  2. ranks the models and decides whether to switch
  3. writes a new AppConfig version (optimistic lock on the version number) and starts the deployment
  The agent picks the change up within about 15 seconds through the AppConfig data plane
  (`StartConfigurationSession` / `GetLatestConfiguration`).
- **The 8 candidates (6 vendors):**
  | Model | Vendor | Strengths |
  |---|---|---|
  | Claude Opus 4.6 | Anthropic | highest quality |
  | Claude Sonnet 4.6 | Anthropic | starting model; near-frontier quality and balance |
  | Claude Haiku 4.5 | Anthropic | fast and cheap Claude |
  | DeepSeek V3.2 | DeepSeek | strong reasoning per dollar |
  | Mistral Large 3 | Mistral AI | fast, low output price |
  | gpt-oss-120b | OpenAI | very cheap open-weight model |
  | Amazon Nova 2 Lite | Amazon | fast, low cost |
  | Qwen3 Next 80B A3B | Qwen / Alibaba | extremely cheap and fast sparse MoE |

  These were chosen from models that, in this account, support **streaming tool use with a guardrail through
  Converse**. The others were excluded for these reasons:
  - Claude 5.x, GPT-5.x and Grok returned AccessDenied.
  - Llama 4, Pixtral and Palmyra don't support streaming tool use.
  - Nova Premier is end-of-life.
  - Nova Pro returned an internal error.
- **How each model is scored** (`scripts/llm_benchmark.py`, results in `data/llm_benchmark.json`):
  - Each model ran the real FR Copilot task twice: the C-007 question with the agent's actual system prompt, the
    actual MCP tool code against the real DynamoDB table, and the live guardrail.
  - **Task score (0-100 rubric):**
    | Check | Points |
    |---|---|
    | fetched profile / accounts / policies | 10 each |
    | ran retirement_projection | 10 |
    | ran life_insurance_needs | 10 |
    | "not on track" verdict | 15 |
    | ~53% funded | 10 |
    | ~$1.43M DIME gap | 15 |
    | mentions the 2036 term expiry | 10 |
  - **Quality** = 60% task score + 40% general-capability prior (instructor-set, illustrative, from public
    leaderboards).
  - **Cost** = measured average tokens × Bedrock on-demand list price (USD per question).
  - **Latency** = measured median end-to-end seconds for the whole multi-tool answer.
  - Each criterion is normalized to 0-100 across the 8 models. Cost uses a log scale because prices span about
    60×. For each criterion, 100 means best.
  - **Score** = weighted sum using the students' weights.
- **"If appropriate" — the switching rules:**
  1. Only models scoring **≥ 80/100 on the task** are *eligible* for live traffic. Below that, the benchmark
     showed they skipped the calculation tools (inventing numbers) or got key facts wrong.
     - **Not eligible:** Mistral Large 3 (78), DeepSeek V3.2 (75) and gpt-oss-120b (65). They are still ranked,
       so students can see why a cheap model at #1 is not picked.
  2. The best eligible model wins.
  3. No switch happens for a lead of under 0.5 points, to avoid flapping on ties.
  4. The decision and its reason are stored in AppConfig and logged by the selector Lambda
     (`llm.preferences.applied`).
- **Benchmark results** (2 runs each, Nov 2 2026):
  | Model | Task | Latency | Cost per question |
  |---|---|---|---|
  | Opus 4.6 | 100 | 37 s | $0.144 |
  | Sonnet 4.6 | 100 | 26 s | $0.094 |
  | Haiku 4.5 | 95 | 11 s | $0.022 |
  | Qwen3 Next | 90 | 15 s | $0.0025 |
  | Nova 2 Lite | 85 | 7.5 s | $0.0062 |
  | Mistral Large 3 | 78 | 8 s | $0.0051 |
  | DeepSeek V3.2 | 75 | 41 s | $0.020 |
  | gpt-oss-120b | 65 | 12.5 s | $0.0028 |
- **Example outcomes** for different weights (quality / cost / latency):
  | Weights | Result |
  |---|---|
  | 50/25/25 | Claude Haiku 4.5 is #1. The app *starts* on Sonnet 4.6 (#4), so the first Evaluate & apply switches to Haiku. |
  | 100/0/0 | Opus 4.6 |
  | 20/60/20 | Qwen3 Next 80B |
  | 20/20/60 | Nova 2 Lite |

### 4.3 Identity and auth (who can call what)
| Hop | Mechanism |
|---|---|
| User → API Gateway | Cognito user-pool access token, checked by the API Gateway Cognito authorizer |
| Chat proxy → Agent runtime | The same user JWT, re-validated by the AgentCore Runtime customJwtAuthorizer (allowed client: `frcopilot-web`) |
| Agent → Gateway | AgentCore Identity OAuth2 credential provider `frcopilot-agent-gateway-m2m` (Cognito client credentials, scope `frcopilot/gateway.invoke`); the Gateway accepts only that client |
| Gateway → MCP runtimes | Credential provider `frcopilot-gateway-mcp-m2m` (scope `frcopilot/mcp.invoke`); the MCP runtimes accept only that client |
| Secrets | Cognito M2M client secrets are in AWS Secrets Manager (`frcopilot/*`), passed to CloudFormation by dynamic reference. AgentCore Identity keeps its own copy in its token vault. Nothing is hard-coded. |

A user's token is rejected by the Gateway (HTTP 403), and an invalid JWT is rejected by the runtime (403). The
end-to-end test proves both.

### 4.4 Student sign-up (self-service IDs)
- The Cognito user pool `frcopilot-reps` has **self sign-up enabled**. Anyone with the site can create as many
  IDs as they like from the "Create an account" link on either app's sign-in page.
- The password policy is Cognito's **minimum**: 6 characters, with no uppercase, number or symbol requirements.
- **No confirmation email or code.** A Cognito **PreSignUp Lambda trigger** (`frcopilot-presignup`) sets
  `autoConfirmUser = true`, and no email or phone attribute is required. Usernames must be 3-32 characters
  (letters, digits, `.`, `_`, `-`) so that they are also valid AgentCore Memory actor ids.
- One ID works in **both** apps. The two apps are separate Cognito app clients (`frcopilot-web` and
  `frcopilot-insights-web`) on the same user pool.
- Long-term memory is per user, so each student ID gets its own remembered preferences.
- Two instructor demo IDs (`rep.jordan` and `rep.casey`) also exist. Their passwords were generated at deploy
  time and stored in Secrets Manager. They are never shown here.

### 4.5 Front end (www)
- Plain HTML/CSS/JavaScript single-page app with no framework, in a private S3 bucket behind CloudFront with
  **Origin Access Control**.
- Features: sign-in and sign-up, a client picker, streaming chat, an MCP tool-call panel, suggested questions,
  and a link to the Insights app.
- Security headers (CSP, HSTS, X-Frame-Options DENY, nosniff) come from a CloudFront response-headers policy.

### 4.6 Why API Gateway + Lambda instead of calling the Runtime directly from the browser
- **Same origin**: no CORS, and the runtime ARN never reaches the browser.
- **Two independent JWT checks**: API Gateway, then AgentCore Runtime.
- One place for input validation, throttling (stage limits), structured logs, X-Ray tracing and retries.
- API Gateway REST **response streaming** keeps token-by-token streaming working end to end.

## 5. The Insights app (insights.awsteach.com)
A separate application with its own stack, domain, bucket, API and Lambdas. It lets students "lift the hood".
- **Static content**: S3 bucket `frcopilot-insights-web-<account>` behind its own CloudFront distribution and
  ACM certificate.
- **Dynamic content**: Lambda only.
  - `frcopilot-insights-chat` (Node.js 22, response streaming): the chatbot.
  - `frcopilot-insights-knowledge` (Python 3.12): serves this file and its version history to the "Knowledge
    file" tab.
- **Knowledge**: this file, `projectknowledge.md`, in the versioned bucket `frcopilot-knowledge-<account>`.
  The chatbot loads it into its system prompt, with a 60-second cache and Bedrock prompt caching.
- **LLM**: **Claude Haiku 4.5** (`us.anthropic.claude-haiku-4-5-20251001-v1:0`) with extended thinking
  (2,048-token budget, interleaved thinking between tool calls). It was chosen because it is fast and cheap for
  Q&A over a document, and it shows students a second model next to the agent's Sonnet 4.6.
- **Live tools** (all read-only, IAM-scoped to FR Copilot resources):
  - `query_logs`: CloudWatch Logs Insights over an allowlist of FR Copilot log groups (agent, both MCP servers,
    Lambdas, Gateway, Memory, OpenTelemetry spans and the model-invocation log).
  - `get_llm_invocations`: reads the **Bedrock model-invocation log**. For each LLM call it shows the question,
    the latest turn (user text or tool results), the **reasoning text**, the response, the tool calls requested,
    tokens and latency. It covers both the FR Copilot agent and the Insights chatbot itself.
  - `get_deployed_resources`: a live CloudFormation inventory, with counts by resource type and stack outputs.
  - `get_bedrock_usage`: Bedrock CloudWatch metrics per model (invocations, tokens, latency, errors).
- **No API keys or secrets are exposed.**
  - The browser only holds the student's own Cognito token.
  - Every AWS call is made by the Lambda's IAM role.
  - Every tool result passes through a redaction filter (JWTs, bearer tokens, AWS access keys, passwords and
    client secrets) before it reaches the model or the browser.
  - Secret values are never written to this file.
- **Bedrock model-invocation logging** is switched on by this stack with a CDK custom resource. It is an
  account-wide, region-wide Bedrock setting.
  - Text payloads go to the CloudWatch log group `/aws/bedrock/frcopilot-model-invocations`.
  - Payloads over 100 KB go to the S3 bucket `frcopilot-llm-invocations-<account>` (14-day lifecycle).
  - The setting is removed again when the stack is deleted.
  - Logged calls carry the caller's IAM role. That is how the tools tell the agent (`frcopilot-agent-us-east-1`)
    apart from the chatbot (`frcopilot-insights-chat-us-east-1`).
- Not visible in the invocation logs: AgentCore Memory's long-term extraction uses an AWS-managed model inside
  the AgentCore service, so those calls are not made from this account.

## 6. Observability
- CloudWatch → **GenAI Observability → Bedrock AgentCore** shows the agent, its sessions and traces (Strands
  spans exported by ADOT to Transaction Search, log group `aws/spans`).
- **Transaction Search / X-Ray**: spans from API Gateway, the Lambdas, the agent, the Gateway, the MCP servers
  and Memory.
- **Vended logs** for the Gateway and Memory go to `/aws/vendedlogs/bedrock-agentcore/frcopilot-*`, and their
  traces go to X-Ray.
- **Runtime logs** are in `/aws/bedrock-agentcore/runtimes/<runtime-id>-DEFAULT`. All services write JSON-lines
  structured logs, for example `invoke.start`, `tool.call`, `llm.reasoning` and `invoke.done`.
- **Dashboard** `FRCopilot-Operations` shows:
  - runtime invocations, latency and errors
  - Claude invocations, tokens and latency
  - guardrail interventions
  - Lambda, API Gateway and DynamoDB metrics
- **Retries**: botocore adaptive retries in every service, MCP tool-list retries in the agent, and the chat proxy
  retries before the first byte.

## 7. Least-privilege IAM (summary)
There is one role per runtime:
- **client-profile-mcp**: read-only access to one table.
- **planning-calc-mcp**: observability permissions only.
- **Agent**: Claude Sonnet 4.6 on one inference profile, ApplyGuardrail on one guardrail, data-plane access to
  one memory, and token-vault access to one credential provider.
- **Gateway**: a token for one credential provider.
- **LLM selector Lambda**: AppConfig read, create-version and start-deployment on the one profile, environment and
  strategy only.
- **Agent** (LLM routing): may invoke only the 8 catalog models, and may read only the one AppConfig profile.
- **Insights chatbot**: S3 read of one object, Bedrock invoke of Haiku 4.5, Logs Insights on the allowlisted log
  groups only, CloudFormation read on the two stacks, and CloudWatch GetMetricData.

Generated policy details appear in the live facts below.

## 8. Testing that was done
- `scripts/e2e_test.py` runs 14 checks through the real public URL:
  - sign-in and the 25 clients
  - the C-007 question calls tools on **both** MCP servers, and the disclaimer is present
  - 401 with no token, 403 on an invalid JWT, and 403 when a user token hits the Gateway
  - the guardrail blocks a "guarantee returns" request and an SSN
- `scripts/memory_test.py` checks that short-term memory carries follow-ups and that long-term preferences are
  saved and applied in a brand-new session.
- Unit tests cover the calculation library.
- The C-007 reference answer:
  - about 53% funded, so **not** on track for 65; about $48.8k/yr (≈$4.1k/month) more saving is needed
  - DIME coverage gap ≈ $1.43M, or ≈ $1.72M if she loses the non-portable group life
  - the term policy expires in 2036, when her youngest child will be 19

## 9. Cost (approximate us-east-1 list prices)
- **Idle**: about $5-10/month, mostly the dashboard, Secrets Manager and the Cognito M2M clients. Runtimes,
  Gateway, Lambda, API Gateway and DynamoDB on-demand cost nothing when idle.
- **Per FR Copilot question**: depends on the live model. Measured: from $0.0025 (Qwen3 Next) to $0.14 (Opus 4.6);
  Sonnet 4.6 is about $0.09 and Haiku 4.5 about $0.02.
- **Per Insights question**: about $0.01-0.03 (Claude Haiku 4.5, with the knowledge file prompt-cached).
- **A class of 30 asking about 10 questions each**: roughly $20-40.

## 10. Deploy and teardown
- Deploy: `./scripts/deploy.sh` runs these steps in order:
  1. unit tests
  2. generate the synthetic data
  3. build the ARM64 runtime zips
  4. bootstrap `FRCopilot-CDKToolkit` (qualifier `frcopilot`)
  5. `cdk deploy` both stacks
  6. seed DynamoDB and create the demo IDs
  7. seed the AppConfig LLM-routing document (only if none exists)
  8. regenerate and upload this knowledge file
- Teardown: `./scripts/teardown.sh` is one command. It destroys both stacks, then removes residue: the ACM
  validation CNAMEs for www and insights, log groups, `frcopilot/*` and AgentCore Identity vault secrets,
  workload identities and the bootstrap stack with its bucket. It also checks that model-invocation logging is
  off, and finishes with a verification pass that must report "clean".
- DNS: only `www`, `insights` and their ACM validation CNAMEs are created in the `awsteach.com` hosted zone. No
  other records are touched.
