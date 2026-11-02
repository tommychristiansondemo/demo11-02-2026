#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { FrCopilotStack } from '../lib/fr-copilot-stack';
import { InsightsStack } from '../lib/insights-stack';

const app = new cdk.App();
cdk.Tags.of(app).add('Project', 'FRCopilot');
cdk.Tags.of(app).add('Owner', 'Instructor');

const env = { account: process.env.CDK_DEFAULT_ACCOUNT, region: 'us-east-1' };
const synthesizer = new cdk.DefaultStackSynthesizer({ qualifier: 'frcopilot' });
const zoneName = app.node.tryGetContext('zoneName') ?? 'awsteach.com';
const hostedZoneId = app.node.tryGetContext('hostedZoneId') ?? 'Z02882605B7GUS0L3VHX';
const domainName = app.node.tryGetContext('domainName') ?? 'www.awsteach.com';
const insightsDomain = app.node.tryGetContext('insightsDomain') ?? 'insights.awsteach.com';
const modelId = app.node.tryGetContext('modelId') ?? 'us.anthropic.claude-sonnet-4-6';

const main = new FrCopilotStack(app, 'FRCopilot', {
  env, synthesizer, zoneName, hostedZoneId, domainName, modelId,
  description: 'FR Copilot - Amazon Bedrock AgentCore class demo (synthetic data only)',
  insightsUrl: `https://${insightsDomain}`,
});

new InsightsStack(app, 'FRCopilotInsights', {
  env, synthesizer, zoneName, hostedZoneId,
  description: 'FR Copilot Insights - separate "under the hood" app: knowledge-file chatbot + live logs and LLM reasoning',
  domainName: insightsDomain,
  modelId: app.node.tryGetContext('insightsModelId') ?? 'us.anthropic.claude-haiku-4-5-20251001-v1:0',
  appModelId: modelId,
  appUrl: `https://${domainName}`,
  userPool: main.userPool,
  appConfig: main.appConfig,
});
