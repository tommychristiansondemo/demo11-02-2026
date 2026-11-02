import * as path from 'path';
import * as crypto from 'crypto';
import * as fs from 'fs';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import {
  aws_apigateway as apigw, aws_appconfig as appconfig, aws_bedrock as bedrock, aws_bedrockagentcore as agentcore, aws_certificatemanager as acm,
  aws_cloudfront as cloudfront, aws_cloudfront_origins as origins, aws_cloudwatch as cw, aws_cognito as cognito,
  aws_dynamodb as dynamodb, aws_iam as iam, aws_lambda as lambda, aws_logs as logs, aws_route53 as route53,
  aws_route53_targets as targets, aws_s3 as s3, aws_s3_assets as s3assets, aws_s3_deployment as s3deploy,
  aws_secretsmanager as secretsmanager,
} from 'aws-cdk-lib';

export interface FrCopilotProps extends cdk.StackProps {
  domainName: string;      // www.awsteach.com
  hostedZoneId: string;
  zoneName: string;        // awsteach.com
  modelId: string;         // Bedrock inference profile id
  insightsUrl: string;     // companion "under the hood" app (separate stack), linked from the UI
}

const ROOT = path.join(__dirname, '..', '..');
// The 8 routable LLMs: single source of truth is the catalog in lambda/llm_selector/ranking.py.
const LLM_CANDIDATES = [...fs.readFileSync(path.join(ROOT, 'lambda', 'llm_selector', 'ranking.py'), 'utf8')
  .matchAll(/"id": "([^"]+)", "name"/g)].map((m) => m[1]);
const BLOCKED = 'I can\'t help with that. I can\'t promise or guarantee investment results or handle sensitive personal '
  + 'identifiers (SSNs, card or bank numbers). For illustration only, not financial advice.';

export class FrCopilotStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly appConfig: { appId: string; envId: string; profileId: string };
  constructor(scope: Construct, id: string, props: FrCopilotProps) {
    super(scope, id, props);
    const { region, account } = this;
    const DESTROY = cdk.RemovalPolicy.DESTROY;

    // ---------------- Data: synthetic clients ----------------
    const table = new dynamodb.TableV2(this, 'Clients', {
      tableName: 'frcopilot-clients',
      partitionKey: { name: 'client_id', type: dynamodb.AttributeType.STRING },
      billing: dynamodb.Billing.onDemand(),
      removalPolicy: DESTROY,
    });

    // ---------------- Cognito: reps/students (humans, self sign-up) + two M2M clients ----------------
    // Students create as many IDs as they like. PreSignUp auto-confirms them, so no confirmation email or code is sent.
    const preSignUp = new lambda.Function(this, 'PreSignUp', {
      functionName: 'frcopilot-presignup',
      description: 'Cognito PreSignUp: auto-confirm self-registered demo IDs (no email/code verification)',
      runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64, handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(ROOT, 'lambda', 'presignup')),
      timeout: cdk.Duration.seconds(5), memorySize: 128,
      logGroup: new logs.LogGroup(this, 'PreSignUpLogs', { logGroupName: '/aws/lambda/frcopilot-presignup', retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: DESTROY }),
    });
    const pool = new cognito.UserPool(this, 'Reps', {
      userPoolName: 'frcopilot-reps',
      selfSignUpEnabled: true,
      signInAliases: { username: true },
      autoVerify: {},
      // Cognito's minimum allowed policy: 6 characters, no character-class requirements.
      passwordPolicy: { minLength: 6, requireDigits: false, requireLowercase: false, requireUppercase: false, requireSymbols: false },
      lambdaTriggers: { preSignUp },
      accountRecovery: cognito.AccountRecovery.NONE,
      featurePlan: cognito.FeaturePlan.ESSENTIALS,
      removalPolicy: DESTROY,
    });
    this.userPool = pool;
    const domainPrefix = `frcopilot-${account}`;
    const domain = pool.addDomain('Domain', { cognitoDomain: { domainPrefix } });
    const gwScope = new cognito.ResourceServerScope({ scopeName: 'gateway.invoke', scopeDescription: 'Agent -> AgentCore Gateway' });
    const mcpScope = new cognito.ResourceServerScope({ scopeName: 'mcp.invoke', scopeDescription: 'Gateway -> MCP runtimes' });
    const rs = pool.addResourceServer('Api', { identifier: 'frcopilot', scopes: [gwScope, mcpScope] });

    const webClient = pool.addClient('Web', {
      userPoolClientName: 'frcopilot-web', generateSecret: false, disableOAuth: true,
      authFlows: { userPassword: true, userSrp: true },
      accessTokenValidity: cdk.Duration.hours(1), idTokenValidity: cdk.Duration.hours(1),
      refreshTokenValidity: cdk.Duration.hours(12), preventUserExistenceErrors: true, enableTokenRevocation: true,
    });
    const m2mClient = (cid: string, name: string, scope: cognito.ResourceServerScope) => pool.addClient(cid, {
      userPoolClientName: name, generateSecret: true, authFlows: {},
      oAuth: { flows: { clientCredentials: true }, scopes: [cognito.OAuthScope.resourceServer(rs, scope)] },
      accessTokenValidity: cdk.Duration.hours(1),
    });
    const agentM2M = m2mClient('AgentM2M', 'frcopilot-agent-to-gateway', gwScope);
    const gatewayM2M = m2mClient('GatewayM2M', 'frcopilot-gateway-to-mcp', mcpScope);

    // M2M client secrets live in Secrets Manager; CloudFormation resolves them via dynamic reference.
    const m2mSecret = (sid: string, name: string, c: cognito.UserPoolClient) => new secretsmanager.Secret(this, sid, {
      secretName: name,
      description: 'FR Copilot OAuth client-credentials app client (Cognito)',
      secretObjectValue: { client_id: cdk.SecretValue.unsafePlainText(c.userPoolClientId), client_secret: c.userPoolClientSecret },
      removalPolicy: DESTROY,
    });
    const agentM2MSecret = m2mSecret('AgentM2MSecret', 'frcopilot/agent-to-gateway-client', agentM2M);
    const gatewayM2MSecret = m2mSecret('GatewayM2MSecret', 'frcopilot/gateway-to-mcp-client', gatewayM2M);

    // Demo rep passwords are generated here; scripts/post_deploy.py creates the Cognito users from these secrets.
    for (const user of ['rep.jordan', 'rep.casey']) {
      new secretsmanager.Secret(this, `DemoUser-${user.replace('.', '-')}`, {
        secretName: `frcopilot/demo-user/${user}`,
        description: 'FR Copilot demo rep login (synthetic demo account)',
        generateSecretString: {
          secretStringTemplate: JSON.stringify({ username: user }), generateStringKey: 'password',
          passwordLength: 14, excludePunctuation: true, requireEachIncludedType: true,
        },
        removalPolicy: DESTROY,
      });
    }

    const discoveryUrl = `https://cognito-idp.${region}.amazonaws.com/${pool.userPoolId}/.well-known/openid-configuration`;
    const issuer = `https://cognito-idp.${region}.amazonaws.com/${pool.userPoolId}`;
    const authBase = `https://${domainPrefix}.auth.${region}.amazoncognito.com`;

    // ---------------- AgentCore Identity: OAuth2 (client-credentials) credential providers ----------------
    const oauthProvider = (pid: string, name: string, secret: secretsmanager.Secret, client: cognito.UserPoolClient) => {
      const p = new agentcore.CfnOAuth2CredentialProvider(this, pid, {
        name,
        credentialProviderVendor: 'CustomOauth2',
        oauth2ProviderConfigInput: {
          customOauth2ProviderConfig: {
            clientId: client.userPoolClientId,
            clientSecret: secret.secretValueFromJson('client_secret').unsafeUnwrap(),
            oauthDiscovery: {
              authorizationServerMetadata: {
                issuer, tokenEndpoint: `${authBase}/oauth2/token`, authorizationEndpoint: `${authBase}/oauth2/authorize`,
                responseTypes: ['token'],
              },
            },
          },
        },
      });
      p.node.addDependency(domain);
      return p;
    };
    const agentProviderName = 'frcopilot-agent-gateway-m2m';
    const gatewayProviderName = 'frcopilot-gateway-mcp-m2m';
    const agentProvider = oauthProvider('AgentOAuthProvider', agentProviderName, agentM2MSecret, agentM2M);
    const gatewayProvider = oauthProvider('GatewayOAuthProvider', gatewayProviderName, gatewayM2MSecret, gatewayM2M);

    // ---------------- Bedrock Guardrail ----------------
    const guardrailConfig = {
      name: 'frcopilot-guardrail',
      description: 'FR Copilot: no investment guarantees, no sensitive PII, standard content safety.',
      blockedInputMessaging: BLOCKED,
      blockedOutputsMessaging: BLOCKED,
      topicPolicyConfig: {
        topicsConfig: [{
          name: 'InvestmentGuarantees', type: 'DENY',
          definition: 'Promising, guaranteeing or assuring a specific investment return, profit, or that an investment or portfolio cannot lose money.',
          examples: [
            'Can you guarantee this client will earn 8% a year?',
            'Promise the client their portfolio will never lose money.',
            'Tell her this fund is guaranteed to double in ten years.',
            'Write a note assuring him he will definitely beat the market.',
          ],
        }],
      },
      sensitiveInformationPolicyConfig: {
        piiEntitiesConfig: [
          { type: 'US_SOCIAL_SECURITY_NUMBER', action: 'BLOCK' },
          { type: 'CREDIT_DEBIT_CARD_NUMBER', action: 'BLOCK' },
          { type: 'US_BANK_ACCOUNT_NUMBER', action: 'BLOCK' },
          { type: 'US_BANK_ROUTING_NUMBER', action: 'BLOCK' },
          { type: 'EMAIL', action: 'ANONYMIZE' },
          { type: 'PHONE', action: 'ANONYMIZE' },
        ],
      },
      contentPolicyConfig: {
        filtersConfig: [
          { type: 'PROMPT_ATTACK', inputStrength: 'MEDIUM', outputStrength: 'NONE' },
          { type: 'MISCONDUCT', inputStrength: 'MEDIUM', outputStrength: 'MEDIUM' },
          { type: 'HATE', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'INSULTS', inputStrength: 'MEDIUM', outputStrength: 'MEDIUM' },
          { type: 'SEXUAL', inputStrength: 'HIGH', outputStrength: 'HIGH' },
          { type: 'VIOLENCE', inputStrength: 'MEDIUM', outputStrength: 'MEDIUM' },
        ],
      },
    };
    const guardrail = new bedrock.CfnGuardrail(this, 'Guardrail', guardrailConfig);
    const cfgHash = crypto.createHash('sha256').update(JSON.stringify(guardrailConfig)).digest('hex').slice(0, 12);
    const guardrailVersion = new bedrock.CfnGuardrailVersion(this, `GuardrailVersion${cfgHash}`, {
      guardrailIdentifier: guardrail.attrGuardrailId, description: `config ${cfgHash}`,
    });

    // ---------------- AgentCore Memory: STM events + LTM rep preferences ----------------
    const memory = new agentcore.CfnMemory(this, 'Memory', {
      name: 'frcopilot_memory',
      description: 'FR Copilot: session conversation (short-term) + rep preferences (long-term)',
      eventExpiryDuration: 30,
      memoryStrategies: [{
        userPreferenceMemoryStrategy: {
          name: 'RepPreferences', description: 'How each rep likes answers (methods, assumptions, detail level)',
          namespaces: ['/preferences/{actorId}'],
        },
      }],
    });

    // ---------------- AppConfig: LLM routing (preferences, 8-model catalog, active model) ----------------
    // The document itself is seeded by scripts/seed_llm_config.py and then changed by the selector Lambda, so CDK
    // owns only the containers (app/env/profile/strategy); redeploys never overwrite students' settings.
    const acApp = new appconfig.CfnApplication(this, 'AppConfigApp', { name: 'frcopilot', description: 'FR Copilot runtime configuration' });
    const acEnv = new appconfig.CfnEnvironment(this, 'AppConfigEnv', { applicationId: acApp.ref, name: 'live', description: 'Live FR Copilot agent' });
    const llmSchema = {
      $schema: 'http://json-schema.org/draft-07/schema#', type: 'object',
      required: ['weights', 'active_model_id', 'candidates'],
      properties: {
        weights: {
          type: 'object', required: ['quality', 'cost', 'latency'],
          properties: { quality: { type: 'integer', minimum: 0, maximum: 100 }, cost: { type: 'integer', minimum: 0, maximum: 100 }, latency: { type: 'integer', minimum: 0, maximum: 100 } },
        },
        active_model_id: { type: 'string', enum: LLM_CANDIDATES },
        candidates: { type: 'array', minItems: 8, maxItems: 8, items: { type: 'object', required: ['id', 'name', 'vendor'], properties: { id: { type: 'string', enum: LLM_CANDIDATES } } } },
      },
    };
    const acProfile = new appconfig.CfnConfigurationProfile(this, 'LlmProfile', {
      applicationId: acApp.ref, name: 'llm-selection', locationUri: 'hosted', type: 'AWS.Freeform',
      description: 'Quality/cost/latency weights, the 8-model catalog with benchmark metrics, and the active agent model',
      validators: [{ type: 'JSON_SCHEMA', content: JSON.stringify(llmSchema) }],
    });
    const acStrategy = new appconfig.CfnDeploymentStrategy(this, 'InstantStrategy', {
      name: 'frcopilot-instant', description: 'All at once, no bake (classroom demo)',
      deploymentDurationInMinutes: 0, finalBakeTimeInMinutes: 0, growthFactor: 100, growthType: 'LINEAR', replicateTo: 'NONE',
    });
    this.appConfig = { appId: acApp.ref, envId: acEnv.ref, profileId: acProfile.ref };
    const llmConfigArn = `arn:aws:appconfig:${region}:${account}:application/${acApp.ref}/environment/${acEnv.ref}/configuration/${acProfile.ref}`;

    // ---------------- IAM roles (least privilege) ----------------
    const agentcoreArn = (res: string) => `arn:aws:bedrock-agentcore:${region}:${account}:${res}`;
    const runtimeRole = (rid: string, name: string) => {
      const role = new iam.Role(this, rid, {
        roleName: `frcopilot-${name}-${region}`,
        description: `FR Copilot AgentCore Runtime execution role (${name})`,
        assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
          conditions: { StringEquals: { 'aws:SourceAccount': account }, ArnLike: { 'aws:SourceArn': agentcoreArn('*') } },
        }),
      });
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'RuntimeLogs',
        actions: ['logs:CreateLogGroup', 'logs:CreateLogStream', 'logs:PutLogEvents', 'logs:DescribeLogStreams'],
        resources: [`arn:aws:logs:${region}:${account}:log-group:/aws/bedrock-agentcore/runtimes/*`],
      }));
      role.addToPolicy(new iam.PolicyStatement({ sid: 'DescribeLogGroups', actions: ['logs:DescribeLogGroups'], resources: ['*'] }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'Tracing',
        actions: ['xray:PutTraceSegments', 'xray:PutTelemetryRecords', 'xray:GetSamplingRules', 'xray:GetSamplingTargets'],
        resources: ['*'],
      }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'Metrics', actions: ['cloudwatch:PutMetricData'], resources: ['*'],
        conditions: { StringEquals: { 'cloudwatch:namespace': 'bedrock-agentcore' } },
      }));
      role.addToPolicy(new iam.PolicyStatement({
        sid: 'WorkloadIdentity',
        actions: ['bedrock-agentcore:GetWorkloadAccessToken', 'bedrock-agentcore:GetWorkloadAccessTokenForJWT',
          'bedrock-agentcore:GetWorkloadAccessTokenForUserId'],
        resources: [agentcoreArn('workload-identity-directory/default'), agentcoreArn('workload-identity-directory/default/workload-identity/*')],
      }));
      return role;
    };
    const oauthTokenStatements = (providerName: string) => [
      new iam.PolicyStatement({
        sid: 'IdentityOAuthToken', actions: ['bedrock-agentcore:GetResourceOauth2Token'],
        resources: [agentcoreArn('token-vault/default'), agentcoreArn(`token-vault/default/oauth2credentialprovider/${providerName}`),
          agentcoreArn('workload-identity-directory/default'), agentcoreArn('workload-identity-directory/default/workload-identity/*')],
      }),
      new iam.PolicyStatement({ sid: 'IdentityVaultSecret', actions: ['secretsmanager:GetSecretValue'], resources: [`arn:aws:secretsmanager:${region}:${account}:secret:bedrock-agentcore-identity!default/oauth2/${providerName}*`] }),
    ];

    const profileRole = runtimeRole('ProfileMcpRole', 'client-profile-mcp');
    profileRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadClients', actions: ['dynamodb:GetItem', 'dynamodb:Scan'], resources: [table.tableArn],
    }));
    const calcRole = runtimeRole('CalcMcpRole', 'planning-calc-mcp');
    const agentRole = runtimeRole('AgentRole', 'agent');
    // Any of the 8 routable models (selected at run time from AppConfig), and nothing else.
    agentRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeRoutableModels', actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [...new Set([props.modelId, ...LLM_CANDIDATES])].flatMap((id) => id.startsWith('us.')
        ? [`arn:aws:bedrock:${region}:${account}:inference-profile/${id}`, `arn:aws:bedrock:*::foundation-model/${id.replace(/^us\./, '')}*`]
        : [`arn:aws:bedrock:${region}::foundation-model/${id}`]),
    }));
    agentRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadLlmRouting', actions: ['appconfig:StartConfigurationSession', 'appconfig:GetLatestConfiguration'],
      resources: [llmConfigArn, `arn:aws:appconfig:${region}:${account}:application/${acApp.ref}`, `arn:aws:appconfig:${region}:${account}:application/${acApp.ref}/*`],
    }));
    agentRole.addToPolicy(new iam.PolicyStatement({ sid: 'ApplyGuardrail', actions: ['bedrock:ApplyGuardrail'], resources: [guardrail.attrGuardrailArn] }));
    agentRole.addToPolicy(new iam.PolicyStatement({
      sid: 'Memory',
      actions: ['bedrock-agentcore:CreateEvent', 'bedrock-agentcore:ListEvents', 'bedrock-agentcore:GetEvent',
        'bedrock-agentcore:RetrieveMemoryRecords', 'bedrock-agentcore:ListMemoryRecords', 'bedrock-agentcore:GetMemoryRecord',
        'bedrock-agentcore:ListSessions', 'bedrock-agentcore:GetMemory'],
      resources: [memory.attrMemoryArn],
    }));
    oauthTokenStatements(agentProviderName).forEach((s) => agentRole.addToPolicy(s));

    const gatewayRole = new iam.Role(this, 'GatewayRole', {
      roleName: `frcopilot-gateway-${region}`,
      description: 'FR Copilot AgentCore Gateway service role',
      assumedBy: new iam.ServicePrincipal('bedrock-agentcore.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': account }, ArnLike: { 'aws:SourceArn': agentcoreArn('*') } },
      }),
    });
    gatewayRole.addToPolicy(new iam.PolicyStatement({
      sid: 'WorkloadIdentity', actions: ['bedrock-agentcore:GetWorkloadAccessToken'],
      resources: [agentcoreArn('workload-identity-directory/default'), agentcoreArn('workload-identity-directory/default/workload-identity/*')],
    }));
    oauthTokenStatements(gatewayProviderName).forEach((s) => gatewayRole.addToPolicy(s));

    // ---------------- AgentCore Runtimes (direct code deployment, ARM64 zips) ----------------
    const otelEnv = (service: string) => ({
      AGENT_OBSERVABILITY_ENABLED: 'true',
      OTEL_PYTHON_DISTRO: 'aws_distro',
      OTEL_PYTHON_CONFIGURATOR: 'aws_configurator',
      OTEL_EXPORTER_OTLP_PROTOCOL: 'http/protobuf',
      OTEL_TRACES_EXPORTER: 'otlp',
      OTEL_LOGS_EXPORTER: 'none',
      OTEL_RESOURCE_ATTRIBUTES: `service.name=${service},deployment.environment=demo`,
    });
    const mkRuntime = (rid: string, name: string, zip: string, protocol: 'MCP' | 'HTTP', role: iam.Role,
      allowedClient: string, env: Record<string, string>, description: string, passAuthHeader = false) => {
      const asset = new s3assets.Asset(this, `${rid}Code`, { path: path.join(__dirname, '..', 'build', zip) });
      const rt = new agentcore.CfnRuntime(this, rid, {
        agentRuntimeName: name,
        description,
        roleArn: role.roleArn,
        protocolConfiguration: protocol,
        agentRuntimeArtifact: {
          codeConfiguration: {
            code: { s3: { bucket: asset.s3BucketName, prefix: asset.s3ObjectKey } },
            entryPoint: ['opentelemetry-instrument', 'main.py'],
            runtime: 'PYTHON_3_12',
          },
        },
        networkConfiguration: { networkMode: 'PUBLIC' },
        authorizerConfiguration: { customJwtAuthorizer: { discoveryUrl, allowedClients: [allowedClient] } },
        environmentVariables: { ...otelEnv(name), ...env },
        lifecycleConfiguration: { idleRuntimeSessionTimeout: 900, maxLifetime: 28800 },
        ...(passAuthHeader ? { requestHeaderConfiguration: { requestHeaderAllowlist: ['Authorization'] } } : {}),
      });
      rt.node.addDependency(role);
      return rt;
    };
    const runtimeUrl = (rt: agentcore.CfnRuntime) =>
      `https://bedrock-agentcore.${region}.amazonaws.com/runtimes/arn%3Aaws%3Abedrock-agentcore%3A${region}%3A${account}%3Aruntime%2F${rt.attrAgentRuntimeId}/invocations?qualifier=DEFAULT`;

    const profileRuntime = mkRuntime('ClientProfileMcp', 'client_profile_mcp', 'client_profile_mcp.zip', 'MCP', profileRole,
      gatewayM2M.userPoolClientId, { CLIENTS_TABLE: table.tableName }, 'client-profile-mcp: synthetic client data (DynamoDB)');
    const calcRuntime = mkRuntime('PlanningCalcMcp', 'planning_calc_mcp', 'planning_calc_mcp.zip', 'MCP', calcRole,
      gatewayM2M.userPoolClientId, {}, 'planning-calc-mcp: deterministic retirement/insurance math');

    // ---------------- AgentCore Gateway: one tool catalog over both MCP servers ----------------
    const gateway = new agentcore.CfnGateway(this, 'Gateway', {
      name: 'frcopilot-gateway',
      description: 'FR Copilot tool catalog: client-profile-mcp + planning-calc-mcp',
      roleArn: gatewayRole.roleArn,
      protocolType: 'MCP',
      protocolConfiguration: { mcp: { supportedVersions: ['2025-03-26', '2025-06-18'] } },
      authorizerType: 'CUSTOM_JWT',
      authorizerConfiguration: { customJwtAuthorizer: { discoveryUrl, allowedClients: [agentM2M.userPoolClientId] } },
    });
    gateway.node.addDependency(gatewayRole);
    const mkTarget = (tid: string, name: string, rt: agentcore.CfnRuntime, description: string) => {
      const t = new agentcore.CfnGatewayTarget(this, tid, {
        gatewayIdentifier: gateway.attrGatewayIdentifier,
        name, description,
        targetConfiguration: { mcp: { mcpServer: { endpoint: runtimeUrl(rt) } } },
        credentialProviderConfigurations: [{
          credentialProviderType: 'OAUTH',
          credentialProvider: { oauthCredentialProvider: { providerArn: gatewayProvider.attrCredentialProviderArn, scopes: ['frcopilot/mcp.invoke'] } },
        }],
      });
      t.node.addDependency(gatewayRole);
      return t;
    };
    const profileTarget = mkTarget('ProfileTarget', 'client-profile-mcp', profileRuntime, 'Synthetic client profiles, policies, accounts');
    const calcTarget = mkTarget('CalcTarget', 'planning-calc-mcp', calcRuntime, 'Retirement projection, insurance needs, RMD, savings gap');
    calcTarget.node.addDependency(profileTarget); // serialize target sync

    // ---------------- Agent runtime (Strands, HTTP protocol, inbound JWT from reps) ----------------
    const agentRuntime = mkRuntime('Agent', 'frcopilot_agent', 'agent.zip', 'HTTP', agentRole, webClient.userPoolClientId, {
      MODEL_ID: props.modelId,
      GATEWAY_URL: gateway.attrGatewayUrl,
      MEMORY_ID: memory.attrMemoryId,
      GUARDRAIL_ID: guardrail.attrGuardrailId,
      GUARDRAIL_VERSION: guardrailVersion.attrVersion,
      IDENTITY_PROVIDER_NAME: agentProviderName,
      GATEWAY_SCOPE: 'frcopilot/gateway.invoke',
      APPCONFIG_APP_ID: acApp.ref, APPCONFIG_ENV_ID: acEnv.ref, APPCONFIG_PROFILE_ID: acProfile.ref,
    }, 'FR Copilot agent (Strands + Claude on Bedrock)', true);
    agentRuntime.node.addDependency(agentProvider, calcTarget);

    // ---------------- Vended logs + traces for Gateway and Memory ----------------
    const vended = (vid: string, resourceArn: string, label: string) => {
      const lg = new logs.LogGroup(this, `${vid}Logs`, {
        logGroupName: `/aws/vendedlogs/bedrock-agentcore/frcopilot-${label}`, retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: DESTROY,
      });
      const logSrc = new logs.CfnDeliverySource(this, `${vid}LogSrc`, { name: `frcopilot-${label}-logs`, logType: 'APPLICATION_LOGS', resourceArn });
      const logDst = new logs.CfnDeliveryDestination(this, `${vid}LogDst`, { name: `frcopilot-${label}-logs`, destinationResourceArn: lg.logGroupArn });
      new logs.CfnDelivery(this, `${vid}LogDelivery`, { deliverySourceName: logSrc.name, deliveryDestinationArn: logDst.attrArn })
        .addDependency(logSrc);
      const trSrc = new logs.CfnDeliverySource(this, `${vid}TraceSrc`, { name: `frcopilot-${label}-traces`, logType: 'TRACES', resourceArn });
      const trDst = new logs.CfnDeliveryDestination(this, `${vid}TraceDst`, { name: `frcopilot-${label}-traces`, deliveryDestinationType: 'XRAY' });
      new logs.CfnDelivery(this, `${vid}TraceDelivery`, { deliverySourceName: trSrc.name, deliveryDestinationArn: trDst.attrArn })
        .addDependency(trSrc);
    };
    vended('GatewayObs', gateway.attrGatewayArn, 'gateway');
    vended('MemoryObs', memory.attrMemoryArn, 'memory');

    // ---------------- API: API Gateway (REST, streaming) + Lambda proxies ----------------
    const chatFn = new lambda.Function(this, 'ChatProxy', {
      functionName: 'frcopilot-chat-proxy',
      description: 'Streams FR Copilot agent responses (forwards the rep JWT to AgentCore Runtime)',
      runtime: lambda.Runtime.NODEJS_22_X, architecture: lambda.Architecture.ARM_64, handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(ROOT, 'lambda', 'chat_proxy')),
      timeout: cdk.Duration.minutes(5), memorySize: 256,
      environment: { AGENT_RUNTIME_ARN: agentRuntime.attrAgentRuntimeArn },
      tracing: lambda.Tracing.ACTIVE,
      logGroup: new logs.LogGroup(this, 'ChatProxyLogs', { logGroupName: '/aws/lambda/frcopilot-chat-proxy', retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: DESTROY }),
    });
    const clientsFn = new lambda.Function(this, 'ClientsApi', {
      functionName: 'frcopilot-clients-api',
      description: 'Lists synthetic clients for the UI picker',
      runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64, handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(ROOT, 'lambda', 'clients_api')),
      timeout: cdk.Duration.seconds(10), memorySize: 256,
      environment: { CLIENTS_TABLE: table.tableName },
      tracing: lambda.Tracing.ACTIVE,
      logGroup: new logs.LogGroup(this, 'ClientsApiLogs', { logGroupName: '/aws/lambda/frcopilot-clients-api', retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: DESTROY }),
    });
    clientsFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['dynamodb:Scan'], resources: [table.tableArn] }));

    const api = new apigw.RestApi(this, 'Api', {
      restApiName: 'frcopilot-api',
      description: 'FR Copilot API (served same-origin through CloudFront /api/*)',
      endpointTypes: [apigw.EndpointType.REGIONAL],
      cloudWatchRole: false,
      deployOptions: { stageName: 'prod', tracingEnabled: true, throttlingRateLimit: 20, throttlingBurstLimit: 40 },
    });
    const authorizer = new apigw.CognitoUserPoolsAuthorizer(this, 'RepAuthorizer', { cognitoUserPools: [pool], authorizerName: 'frcopilot-reps' });
    const auth = { authorizer, authorizationType: apigw.AuthorizationType.COGNITO, authorizationScopes: ['aws.cognito.signin.user.admin'] };
    const apiRoot = api.root.addResource('api');
    apiRoot.addResource('chat').addMethod('POST', new apigw.LambdaIntegration(chatFn, {
      proxy: true, responseTransferMode: apigw.ResponseTransferMode.STREAM, timeout: cdk.Duration.minutes(5),
    }), auth);
    apiRoot.addResource('clients').addMethod('GET', new apigw.LambdaIntegration(clientsFn, { proxy: true }), auth);

    // LLM routing API: read the ranking / apply new preferences (writes + deploys AppConfig).
    const selectorFn = new lambda.Function(this, 'LlmSelector', {
      functionName: 'frcopilot-llm-selector',
      description: 'Ranks the 8 routable LLMs for the quality/cost/latency weights and switches the live agent model via AppConfig',
      runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64, handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(ROOT, 'lambda', 'llm_selector'), { exclude: ['__pycache__'] }),
      timeout: cdk.Duration.seconds(25), memorySize: 256, tracing: lambda.Tracing.ACTIVE,
      environment: { APPCONFIG_APP_ID: acApp.ref, APPCONFIG_ENV_ID: acEnv.ref, APPCONFIG_PROFILE_ID: acProfile.ref, APPCONFIG_STRATEGY_ID: acStrategy.ref },
      logGroup: new logs.LogGroup(this, 'LlmSelectorLogs', { logGroupName: '/aws/lambda/frcopilot-llm-selector', retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: DESTROY }),
    });
    const acAppArn = `arn:aws:appconfig:${region}:${account}:application/${acApp.ref}`;
    // AppConfig authorizes some of these calls against the application ARN, so scope to this one application.
    selectorFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['appconfig:ListHostedConfigurationVersions', 'appconfig:GetHostedConfigurationVersion', 'appconfig:CreateHostedConfigurationVersion',
        'appconfig:StartDeployment', 'appconfig:ListDeployments', 'appconfig:GetDeployment'],
      resources: [acAppArn, `${acAppArn}/*`, `arn:aws:appconfig:${region}:${account}:deploymentstrategy/${acStrategy.ref}`],
    }));
    const llmRes = apiRoot.addResource('llm');
    const selectorInt = new apigw.LambdaIntegration(selectorFn, { proxy: true });
    llmRes.addMethod('GET', selectorInt, auth);
    llmRes.addMethod('POST', selectorInt, auth);

    // ---------------- Web: S3 (private) + CloudFront (OAC) + ACM + Route 53 ----------------
    const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', { hostedZoneId: props.hostedZoneId, zoneName: props.zoneName });
    const cert = new acm.Certificate(this, 'Cert', {
      domainName: props.domainName, validation: acm.CertificateValidation.fromDns(zone),
    });
    const siteBucket = new s3.Bucket(this, 'Site', {
      bucketName: `frcopilot-web-${account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true, encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: DESTROY, autoDeleteObjects: true,
    });
    const csp = "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; "
      + "font-src https://fonts.gstatic.com; img-src 'self' data:; connect-src 'self' https://cognito-idp."
      + `${region}.amazonaws.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`;
    const headers = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      responseHeadersPolicyName: 'frcopilot-security-headers',
      securityHeadersBehavior: {
        contentSecurityPolicy: { contentSecurityPolicy: csp, override: true },
        strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: false, override: true },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
      },
    });
    const dist = new cloudfront.Distribution(this, 'Cdn', {
      comment: 'FR Copilot web + API',
      domainNames: [props.domainName], certificate: cert,
      defaultRootObject: 'index.html',
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021,
      httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED,
        responseHeadersPolicy: headers,
      },
      additionalBehaviors: {
        '/api/*': {
          origin: new origins.RestApiOrigin(api, { readTimeout: cdk.Duration.seconds(60) }),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY,
          allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER,
          responseHeadersPolicy: headers,
        },
      },
    });
    const recordName = props.domainName.replace(`.${props.zoneName}`, '');
    new route53.ARecord(this, 'WwwA', { zone, recordName, target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(dist)) });
    new route53.AaaaRecord(this, 'WwwAAAA', { zone, recordName, target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(dist)) });

    new s3deploy.BucketDeployment(this, 'DeploySite', {
      sources: [
        s3deploy.Source.asset(path.join(ROOT, 'web')),
        s3deploy.Source.jsonData('config.json', { region, userPoolId: pool.userPoolId, clientId: webClient.userPoolClientId, insightsUrl: props.insightsUrl }),
      ],
      destinationBucket: siteBucket, distribution: dist, distributionPaths: ['/*'], memoryLimit: 512,
      logGroup: new logs.LogGroup(this, 'DeploySiteLogs', { retention: logs.RetentionDays.ONE_DAY, removalPolicy: DESTROY }),
    });

    // ---------------- Operations dashboard ----------------
    const search = (expr: string, label: string, stat = 'Sum') => new cw.MathExpression({
      expression: `SEARCH('${expr}', '${stat}', 300)`, label, usingMetrics: {}, period: cdk.Duration.minutes(5),
    });
    const consoleBase = `https://${region}.console.aws.amazon.com/cloudwatch/home?region=${region}`;
    new cw.Dashboard(this, 'Dashboard', {
      dashboardName: 'FRCopilot-Operations',
      defaultInterval: cdk.Duration.hours(3),
      widgets: [
        [new cw.TextWidget({
          width: 24, height: 3,
          markdown: `## FR Copilot operations\nAgent traces, sessions and tool spans: [GenAI Observability → Bedrock AgentCore](${consoleBase}#gen-ai-observability/agent-core) · `
            + `[Transaction Search](${consoleBase}#xray:traces/query) · [Runtime logs](${consoleBase}#logsV2:log-groups$3FlogGroupNameFilter$3D$252Faws$252Fbedrock-agentcore)\n`
            + 'All data is synthetic. Agent model is chosen at run time from AppConfig (frcopilot / live / llm-selection); starting model ' + props.modelId,
        })],
        [
          new cw.GraphWidget({ title: 'AgentCore runtime invocations', width: 8, left: [search('{AWS/Bedrock-AgentCore} MetricName="Invocations"', 'Invocations')] }),
          new cw.GraphWidget({ title: 'AgentCore runtime latency (avg ms)', width: 8, left: [search('{AWS/Bedrock-AgentCore} MetricName="Latency"', 'Latency', 'Average')] }),
          new cw.GraphWidget({ title: 'AgentCore errors / throttles', width: 8, left: [
            search('{AWS/Bedrock-AgentCore} MetricName="SystemErrors"', 'SystemErrors'),
            search('{AWS/Bedrock-AgentCore} MetricName="UserErrors"', 'UserErrors'),
            search('{AWS/Bedrock-AgentCore} MetricName="Throttles"', 'Throttles')] }),
        ],
        [
          new cw.GraphWidget({ title: 'LLM invocations by model (all routable models)', width: 8,
            left: [search('{AWS/Bedrock,ModelId} MetricName="Invocations"', 'Invocations')] }),
          new cw.GraphWidget({ title: 'LLM output tokens by model', width: 8,
            left: [search('{AWS/Bedrock,ModelId} MetricName="OutputTokenCount"', 'Output tokens')] }),
          new cw.GraphWidget({ title: 'Guardrail invocations / interventions', width: 8, left: [
            search('{AWS/Bedrock/Guardrails} MetricName="Invocations"', 'Guardrail invocations'),
            search('{AWS/Bedrock/Guardrails} MetricName="InvocationsIntervened"', 'Interventions')] }),
        ],
        [
          new cw.GraphWidget({ title: 'Chat proxy Lambda', width: 8, left: [chatFn.metricInvocations(), chatFn.metricErrors()], right: [chatFn.metricDuration({ statistic: 'p90' })] }),
          new cw.GraphWidget({ title: 'API Gateway', width: 8, left: [api.metricCount(), api.metricClientError(), api.metricServerError()] }),
          new cw.GraphWidget({ title: 'DynamoDB reads', width: 8, left: [table.metricConsumedReadCapacityUnits()] }),
        ],
      ],
    });

    // ---------------- Outputs ----------------
    const out = (k: string, v: string) => new cdk.CfnOutput(this, k, { value: v });
    out('SiteUrl', `https://${props.domainName}`);
    out('CloudFrontDomain', dist.distributionDomainName);
    out('DistributionId', dist.distributionId);
    out('UserPoolId', pool.userPoolId);
    out('PreSignUpFunction', preSignUp.functionName);
    out('WebClientId', webClient.userPoolClientId);
    out('ApiUrl', api.url);
    out('AgentRuntimeArn', agentRuntime.attrAgentRuntimeArn);
    out('ClientProfileRuntimeArn', profileRuntime.attrAgentRuntimeArn);
    out('PlanningCalcRuntimeArn', calcRuntime.attrAgentRuntimeArn);
    out('GatewayUrl', gateway.attrGatewayUrl);
    out('GatewayId', gateway.attrGatewayIdentifier);
    out('MemoryId', memory.attrMemoryId);
    out('AgentModelId', props.modelId);
    out('AppConfigAppId', acApp.ref);
    out('AppConfigEnvId', acEnv.ref);
    out('AppConfigProfileId', acProfile.ref);
    out('AppConfigStrategyId', acStrategy.ref);
    out('LlmCandidates', LLM_CANDIDATES.join(','));
    out('GuardrailId', guardrail.attrGuardrailId);
    out('GuardrailVersion', guardrailVersion.attrVersion);
    out('ClientsTable', table.tableName);
    out('SiteBucket', siteBucket.bucketName);
    out('CertificateArn', cert.certificateArn);
    out('DashboardUrl', `${consoleBase}#dashboards/dashboard/FRCopilot-Operations`);
  }
}
