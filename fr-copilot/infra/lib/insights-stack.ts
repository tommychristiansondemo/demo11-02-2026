import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import {
  aws_apigateway as apigw, aws_certificatemanager as acm, aws_cloudfront as cloudfront, aws_cloudfront_origins as origins,
  aws_cognito as cognito, aws_iam as iam, aws_lambda as lambda, aws_logs as logs, aws_route53 as route53,
  aws_route53_targets as targets, aws_s3 as s3, aws_s3_deployment as s3deploy, custom_resources as cr,
} from 'aws-cdk-lib';

/**
 * FR Copilot Insights: a separate "under the hood" app for students.
 * Static site in S3 (CloudFront + OAC), all dynamic content from Lambda (chatbot + knowledge file API).
 * The chatbot (Claude Haiku 4.5 on Bedrock) reads projectknowledge.md from its own S3 bucket and can query live
 * CloudWatch logs, Bedrock model-invocation logs (prompts + reasoning), CloudFormation inventory and Bedrock metrics.
 */
export interface InsightsProps extends cdk.StackProps {
  domainName: string;        // insights.awsteach.com
  hostedZoneId: string;
  zoneName: string;
  modelId: string;           // chatbot model (inference profile id)
  appModelId: string;        // FR Copilot agent model, for usage metrics
  appUrl: string;            // https://www.awsteach.com
  userPool: cognito.IUserPool; // shared with FR Copilot: one self-created student ID works in both apps
  appConfig: { appId: string; envId: string; profileId: string }; // FR Copilot LLM routing (read-only here)
}

const ROOT = path.join(__dirname, '..', '..');
export const INVOCATION_LOG_GROUP = '/aws/bedrock/frcopilot-model-invocations';

export class InsightsStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: InsightsProps) {
    super(scope, id, props);
    const { region, account } = this;
    const DESTROY = cdk.RemovalPolicy.DESTROY;

    // ---------------- Knowledge file bucket (projectknowledge.md, versioned so every update is kept) ----------------
    const knowledgeBucket = new s3.Bucket(this, 'Knowledge', {
      bucketName: `frcopilot-knowledge-${account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true, encryption: s3.BucketEncryption.S3_MANAGED,
      versioned: true, removalPolicy: DESTROY, autoDeleteObjects: true,
    });

    // ---------------- Bedrock model-invocation logging (prompts, responses, reasoning) ----------------
    // Account+region-wide Bedrock setting; created here, removed on stack delete.
    const invLogs = new logs.LogGroup(this, 'InvocationLogs', {
      logGroupName: INVOCATION_LOG_GROUP, retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: DESTROY,
    });
    const invBucket = new s3.Bucket(this, 'InvocationLarge', {
      bucketName: `frcopilot-llm-invocations-${account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true, encryption: s3.BucketEncryption.S3_MANAGED,
      lifecycleRules: [{ expiration: cdk.Duration.days(14) }], removalPolicy: DESTROY, autoDeleteObjects: true,
    });
    const bedrockCond = { StringEquals: { 'aws:SourceAccount': account }, ArnLike: { 'aws:SourceArn': `arn:aws:bedrock:${region}:${account}:*` } };
    invBucket.addToResourcePolicy(new iam.PolicyStatement({
      sid: 'BedrockLargeInvocationPayloads', principals: [new iam.ServicePrincipal('bedrock.amazonaws.com')],
      actions: ['s3:PutObject'], resources: [invBucket.arnForObjects('*')], conditions: bedrockCond,
    }));
    const invRole = new iam.Role(this, 'InvocationLoggingRole', {
      roleName: `frcopilot-bedrock-invocation-logging-${region}`,
      description: 'Lets Amazon Bedrock write model-invocation logs for the FR Copilot class demo',
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', { conditions: bedrockCond }),
    });
    invRole.addToPolicy(new iam.PolicyStatement({
      actions: ['logs:CreateLogStream', 'logs:PutLogEvents'], resources: [`${invLogs.logGroupArn}`, `${invLogs.logGroupArn}:log-stream:*`],
    }));
    invRole.addToPolicy(new iam.PolicyStatement({ actions: ['s3:PutObject'], resources: [invBucket.arnForObjects('*')] }));
    const loggingConfig = {
      loggingConfig: {
        cloudWatchConfig: {
          logGroupName: invLogs.logGroupName, roleArn: invRole.roleArn,
          largeDataDeliveryS3Config: { bucketName: invBucket.bucketName, keyPrefix: 'large' },
        },
        textDataDeliveryEnabled: true, imageDataDeliveryEnabled: false, embeddingDataDeliveryEnabled: false,
      },
    };
    const invLogging = new cr.AwsCustomResource(this, 'ModelInvocationLogging', {
      resourceType: 'Custom::BedrockModelInvocationLogging',
      onCreate: { service: 'Bedrock', action: 'PutModelInvocationLoggingConfiguration', parameters: loggingConfig,
        physicalResourceId: cr.PhysicalResourceId.of('frcopilot-model-invocation-logging') },
      onUpdate: { service: 'Bedrock', action: 'PutModelInvocationLoggingConfiguration', parameters: loggingConfig,
        physicalResourceId: cr.PhysicalResourceId.of('frcopilot-model-invocation-logging') },
      onDelete: { service: 'Bedrock', action: 'DeleteModelInvocationLoggingConfiguration' },
      policy: cr.AwsCustomResourcePolicy.fromStatements([
        new iam.PolicyStatement({ actions: ['bedrock:PutModelInvocationLoggingConfiguration', 'bedrock:DeleteModelInvocationLoggingConfiguration'], resources: ['*'] }),
        new iam.PolicyStatement({ actions: ['iam:PassRole'], resources: [invRole.roleArn] }),
      ]),
      logGroup: new logs.LogGroup(this, 'InvocationLoggingCrLogs', { logGroupName: '/aws/lambda/frcopilot-insights-invocation-logging-cr', retention: logs.RetentionDays.ONE_DAY, removalPolicy: DESTROY }),
      installLatestAwsSdk: false,
    });
    invLogging.node.addDependency(invRole, invBucket, invLogs);

    // ---------------- Lambda: chatbot (Node 22, response streaming) ----------------
    const lg = (name: string) => `arn:aws:logs:${region}:${account}:log-group:${name}`;
    const readableLogGroups = [
      '/aws/bedrock-agentcore/runtimes/frcopilot_agent-*', '/aws/bedrock-agentcore/runtimes/client_profile_mcp-*',
      '/aws/bedrock-agentcore/runtimes/planning_calc_mcp-*', '/aws/lambda/frcopilot-*',
      '/aws/vendedlogs/bedrock-agentcore/frcopilot-*', INVOCATION_LOG_GROUP, 'aws/spans',
    ];
    const chatRole = new iam.Role(this, 'ChatRole', {
      roleName: `frcopilot-insights-chat-${region}`,
      description: 'FR Copilot Insights chatbot Lambda: read-only access to the knowledge file, logs, inventory and metrics',
      assumedBy: new iam.ServicePrincipal('lambda.amazonaws.com'),
      managedPolicies: [iam.ManagedPolicy.fromAwsManagedPolicyName('AWSXRayDaemonWriteAccess')],
    });
    const chatLogs = new logs.LogGroup(this, 'ChatLogs', { logGroupName: '/aws/lambda/frcopilot-insights-chat', retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: DESTROY });
    chatRole.addToPolicy(new iam.PolicyStatement({ sid: 'OwnLogs', actions: ['logs:CreateLogStream', 'logs:PutLogEvents'], resources: [chatLogs.logGroupArn, `${chatLogs.logGroupArn}:*`] }));
    chatRole.addToPolicy(new iam.PolicyStatement({ sid: 'ReadKnowledge', actions: ['s3:GetObject'], resources: [knowledgeBucket.arnForObjects('projectknowledge.md')] }));
    chatRole.addToPolicy(new iam.PolicyStatement({ sid: 'ReadLargeInvocationPayloads', actions: ['s3:GetObject'], resources: [invBucket.arnForObjects('*')] }));
    const modelBase = props.modelId.replace(/^(us|global)\./, '');
    chatRole.addToPolicy(new iam.PolicyStatement({
      sid: 'InvokeChatModel', actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
      resources: [`arn:aws:bedrock:${region}:${account}:inference-profile/${props.modelId}`, `arn:aws:bedrock:*::foundation-model/${modelBase}`],
    }));
    chatRole.addToPolicy(new iam.PolicyStatement({
      sid: 'QueryDemoLogs', actions: ['logs:StartQuery', 'logs:FilterLogEvents'],
      resources: readableLogGroups.flatMap((n) => [lg(n), `${lg(n)}:*`]),
    }));
    chatRole.addToPolicy(new iam.PolicyStatement({ sid: 'QueryResults', actions: ['logs:GetQueryResults', 'logs:StopQuery', 'logs:DescribeLogGroups'], resources: ['*'] }));
    chatRole.addToPolicy(new iam.PolicyStatement({
      sid: 'Inventory', actions: ['cloudformation:DescribeStacks', 'cloudformation:ListStackResources'],
      resources: ['FRCopilot', 'FRCopilotInsights'].map((s) => `arn:aws:cloudformation:${region}:${account}:stack/${s}/*`),
    }));
    chatRole.addToPolicy(new iam.PolicyStatement({ sid: 'Metrics', actions: ['cloudwatch:GetMetricData'], resources: ['*'] }));
    const acAppArn = `arn:aws:appconfig:${region}:${account}:application/${props.appConfig.appId}`;
    chatRole.addToPolicy(new iam.PolicyStatement({
      sid: 'ReadLlmRouting', actions: ['appconfig:ListHostedConfigurationVersions', 'appconfig:GetHostedConfigurationVersion', 'appconfig:ListDeployments'],
      resources: [acAppArn, `${acAppArn}/*`],
    }));

    const chatFn = new lambda.Function(this, 'Chat', {
      functionName: 'frcopilot-insights-chat',
      description: 'FR Copilot Insights chatbot: Claude on Bedrock + knowledge file + live logs/reasoning/inventory tools',
      runtime: lambda.Runtime.NODEJS_22_X, architecture: lambda.Architecture.ARM_64, handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(ROOT, 'lambda', 'insights_chat')),
      role: chatRole, timeout: cdk.Duration.minutes(5), memorySize: 512, tracing: lambda.Tracing.ACTIVE, logGroup: chatLogs,
      environment: {
        MODEL_ID: props.modelId, APP_MODEL_ID: props.appModelId,
        KNOWLEDGE_BUCKET: knowledgeBucket.bucketName, KNOWLEDGE_KEY: 'projectknowledge.md',
        APPCONFIG_APP_ID: props.appConfig.appId, APPCONFIG_ENV_ID: props.appConfig.envId, APPCONFIG_PROFILE_ID: props.appConfig.profileId,
        INVOCATION_LOG_GROUP, AGENT_ROLE_NAME: `frcopilot-agent-${region}`, CHAT_ROLE_NAME: `frcopilot-insights-chat-${region}`,
      },
    });

    // ---------------- Lambda: knowledge file API ----------------
    const knowledgeFn = new lambda.Function(this, 'KnowledgeApi', {
      functionName: 'frcopilot-insights-knowledge',
      description: 'Serves projectknowledge.md (and its version history) to the Insights UI',
      runtime: lambda.Runtime.PYTHON_3_12, architecture: lambda.Architecture.ARM_64, handler: 'index.handler',
      code: lambda.Code.fromAsset(path.join(ROOT, 'lambda', 'insights_knowledge')),
      timeout: cdk.Duration.seconds(10), memorySize: 256, tracing: lambda.Tracing.ACTIVE,
      environment: { KNOWLEDGE_BUCKET: knowledgeBucket.bucketName, KNOWLEDGE_KEY: 'projectknowledge.md' },
      logGroup: new logs.LogGroup(this, 'KnowledgeLogs', { logGroupName: '/aws/lambda/frcopilot-insights-knowledge', retention: logs.RetentionDays.TWO_WEEKS, removalPolicy: DESTROY }),
    });
    knowledgeFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['s3:GetObject', 's3:GetObjectVersion'], resources: [knowledgeBucket.arnForObjects('projectknowledge.md')] }));
    knowledgeFn.addToRolePolicy(new iam.PolicyStatement({ actions: ['s3:ListBucketVersions'], resources: [knowledgeBucket.bucketArn] }));

    // ---------------- API Gateway (REST, Cognito authorizer, streaming) ----------------
    const webClient = new cognito.UserPoolClient(this, 'InsightsWeb', {
      userPool: props.userPool, userPoolClientName: 'frcopilot-insights-web', generateSecret: false, disableOAuth: true,
      authFlows: { userPassword: true, userSrp: true },
      accessTokenValidity: cdk.Duration.hours(1), idTokenValidity: cdk.Duration.hours(1), refreshTokenValidity: cdk.Duration.hours(12),
      preventUserExistenceErrors: true, enableTokenRevocation: true,
    });
    const api = new apigw.RestApi(this, 'Api', {
      restApiName: 'frcopilot-insights-api',
      description: 'FR Copilot Insights API (served same-origin through CloudFront /api/*)',
      endpointTypes: [apigw.EndpointType.REGIONAL], cloudWatchRole: false,
      deployOptions: { stageName: 'prod', tracingEnabled: true, throttlingRateLimit: 10, throttlingBurstLimit: 20 },
    });
    const authorizer = new apigw.CognitoUserPoolsAuthorizer(this, 'Authorizer', { cognitoUserPools: [props.userPool], authorizerName: 'frcopilot-insights' });
    const auth = { authorizer, authorizationType: apigw.AuthorizationType.COGNITO, authorizationScopes: ['aws.cognito.signin.user.admin'] };
    const apiRoot = api.root.addResource('api');
    apiRoot.addResource('chat').addMethod('POST', new apigw.LambdaIntegration(chatFn, {
      proxy: true, responseTransferMode: apigw.ResponseTransferMode.STREAM, timeout: cdk.Duration.minutes(5),
    }), auth);
    apiRoot.addResource('knowledge').addMethod('GET', new apigw.LambdaIntegration(knowledgeFn, { proxy: true }), auth);

    // ---------------- Static site: S3 + CloudFront (OAC) + ACM + Route 53 ----------------
    const zone = route53.HostedZone.fromHostedZoneAttributes(this, 'Zone', { hostedZoneId: props.hostedZoneId, zoneName: props.zoneName });
    const cert = new acm.Certificate(this, 'Cert', { domainName: props.domainName, validation: acm.CertificateValidation.fromDns(zone) });
    const siteBucket = new s3.Bucket(this, 'Site', {
      bucketName: `frcopilot-insights-web-${account}`,
      blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL, enforceSSL: true, encryption: s3.BucketEncryption.S3_MANAGED,
      removalPolicy: DESTROY, autoDeleteObjects: true,
    });
    const csp = "default-src 'self'; script-src 'self'; style-src 'self' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
      + `img-src 'self' data:; connect-src 'self' https://cognito-idp.${region}.amazonaws.com; frame-ancestors 'none'; base-uri 'none'; form-action 'self'`;
    const headers = new cloudfront.ResponseHeadersPolicy(this, 'SecurityHeaders', {
      responseHeadersPolicyName: 'frcopilot-insights-security-headers',
      securityHeadersBehavior: {
        contentSecurityPolicy: { contentSecurityPolicy: csp, override: true },
        strictTransportSecurity: { accessControlMaxAge: cdk.Duration.days(365), includeSubdomains: false, override: true },
        contentTypeOptions: { override: true },
        frameOptions: { frameOption: cloudfront.HeadersFrameOption.DENY, override: true },
        referrerPolicy: { referrerPolicy: cloudfront.HeadersReferrerPolicy.STRICT_ORIGIN_WHEN_CROSS_ORIGIN, override: true },
      },
    });
    const dist = new cloudfront.Distribution(this, 'Cdn', {
      comment: 'FR Copilot Insights web + API',
      domainNames: [props.domainName], certificate: cert, defaultRootObject: 'index.html',
      minimumProtocolVersion: cloudfront.SecurityPolicyProtocol.TLS_V1_2_2021, httpVersion: cloudfront.HttpVersion.HTTP2_AND_3,
      priceClass: cloudfront.PriceClass.PRICE_CLASS_100,
      defaultBehavior: {
        origin: origins.S3BucketOrigin.withOriginAccessControl(siteBucket),
        viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.REDIRECT_TO_HTTPS,
        cachePolicy: cloudfront.CachePolicy.CACHING_OPTIMIZED, responseHeadersPolicy: headers,
      },
      additionalBehaviors: {
        '/api/*': {
          origin: new origins.RestApiOrigin(api, { readTimeout: cdk.Duration.seconds(60) }),
          viewerProtocolPolicy: cloudfront.ViewerProtocolPolicy.HTTPS_ONLY, allowedMethods: cloudfront.AllowedMethods.ALLOW_ALL,
          cachePolicy: cloudfront.CachePolicy.CACHING_DISABLED,
          originRequestPolicy: cloudfront.OriginRequestPolicy.ALL_VIEWER_EXCEPT_HOST_HEADER, responseHeadersPolicy: headers,
        },
      },
    });
    const recordName = props.domainName.replace(`.${props.zoneName}`, '');
    new route53.ARecord(this, 'InsightsA', { zone, recordName, target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(dist)) });
    new route53.AaaaRecord(this, 'InsightsAAAA', { zone, recordName, target: route53.RecordTarget.fromAlias(new targets.CloudFrontTarget(dist)) });
    new s3deploy.BucketDeployment(this, 'DeploySite', {
      sources: [
        s3deploy.Source.asset(path.join(ROOT, 'insights_web')),
        s3deploy.Source.jsonData('config.json', { region, userPoolId: props.userPool.userPoolId, clientId: webClient.userPoolClientId, appUrl: props.appUrl }),
      ],
      destinationBucket: siteBucket, distribution: dist, distributionPaths: ['/*'], memoryLimit: 512,
      logGroup: new logs.LogGroup(this, 'DeploySiteLogs', { retention: logs.RetentionDays.ONE_DAY, removalPolicy: DESTROY }),
    });

    const out = (k: string, v: string) => new cdk.CfnOutput(this, k, { value: v });
    out('InsightsUrl', `https://${props.domainName}`);
    out('KnowledgeBucket', knowledgeBucket.bucketName);
    out('KnowledgeObject', `s3://${knowledgeBucket.bucketName}/projectknowledge.md`);
    out('InvocationLogGroup', invLogs.logGroupName);
    out('InvocationLargePayloadBucket', invBucket.bucketName);
    out('ChatbotModelId', props.modelId);
    out('InsightsWebClientId', webClient.userPoolClientId);
    out('InsightsApiUrl', api.url);
    out('InsightsDistributionId', dist.distributionId);
    out('InsightsSiteBucket', siteBucket.bucketName);
  }
}
