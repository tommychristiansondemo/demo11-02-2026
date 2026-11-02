## Change log (newest first)
- **2026-11-02, release 3: LLM routing.**
  - **Benchmark:** 8 Bedrock LLMs from 6 vendors were benchmarked on the real FR Copilot task.
  - **AppConfig:** a new configuration (`frcopilot/live/llm-selection`) holds the weights (starting at quality 50 /
    cost 25 / latency 25), the catalog and the active model.
  - **Front end:** a slider panel lets any student re-weight the criteria and *Evaluate & apply*. The new
    `frcopilot-llm-selector` Lambda ranks the models and, if appropriate, switches the live agent's model with
    no redeploy.
  - **Agent:** it now reads its model from AppConfig.
  - **Insights:** the chatbot gained `get_llm_routing`, and its usage metrics now cover all 8 models.
- **2026-11-02, release 2**:
  - **Student self sign-up**: unlimited IDs, Cognito minimum password policy and auto-confirm through a PreSignUp
    Lambda, so no confirmation email is sent.
  - **Insights app**: a new separate stack `FRCopilotInsights` at insights.awsteach.com, with the knowledge-file
    chatbot on Claude Haiku 4.5 and live log, reasoning, inventory and metrics tools.
  - **Reasoning logging**: Bedrock model-invocation logging is now on, and extended thinking is enabled on the
    FR Copilot agent (adaptive) and on the chatbot.
  - **Links**: the main app now links to Insights.
- **2026-11-02, release 1**: initial FR Copilot deployment:
  - two MCP servers on AgentCore Runtime, behind AgentCore Gateway
  - a Strands agent on Claude Sonnet 4.6 with Guardrail and Memory
  - Cognito sign-in, AgentCore Identity M2M OAuth, and an SPA on S3 + CloudFront at www.awsteach.com
  - the operations dashboard
