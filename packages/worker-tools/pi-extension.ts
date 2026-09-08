import type { ExtensionAPI } from '@earendil-works/pi-coding-agent';
import { Type } from 'typebox';
import { WorkerClient, formatToolOutput } from './src/client';
import { loadConfig } from './src/config';

/** Per-launch configuration only: deliberately no global config or background polling. */
export default async function orchestratorWorker(pi: ExtensionAPI): Promise<void> {
  pi.registerFlag('orchestrator-credential-file', {
    description: 'Owner-only file containing this worker\'s scoped Orchestrator credential', type: 'string',
  });
  pi.registerFlag('orchestrator-url', {
    description: 'Orchestrator control URL', type: 'string',
  });
  const credentialFlag = pi.getFlag('orchestrator-credential-file');
  const urlFlag = pi.getFlag('orchestrator-url');
  const credentialFile = typeof credentialFlag === 'string' ? credentialFlag : undefined;
  const url = typeof urlFlag === 'string' ? urlFlag : undefined;
  if (!credentialFile && !process.env.ORCHESTRATOR_WORKER_CREDENTIAL_FILE && !process.env.ORCHESTRATOR_WORKER_TOKEN) return;
  const client = new WorkerClient(await loadConfig({ credentialFile, url }));

  pi.registerTool({
    name: 'read_inbox', label: 'Orchestrator inbox',
    description: 'Read recent messages assigned to this worker. Non-destructive, output capped at 40 KiB. Herdr owns wakeup; do not poll repeatedly.',
    promptSnippet: 'Read messages from the orchestrator',
    parameters: Type.Object({}, { additionalProperties: false }),
    async execute(_toolCallId, _params, signal) {
      const result = await client.inbox(signal);
      return { content: [{ type: 'text', text: formatToolOutput(result) }], details: {} };
    },
  });
  pi.registerTool({
    name: 'reply_to_orchestrator', label: 'Reply to orchestrator',
    description: 'Send progress, a question, a result, or an error to the orchestrator. Choose a stable reply id and reuse exactly the same id/payload on retries. replyTo is an inbox message id. Omit secrets.',
    promptSnippet: 'Send a durable reply to the orchestrator',
    promptGuidelines: ['Use reply_to_orchestrator to report task results or questions instead of assuming terminal output is relayed to the phone. Never invent a success result or repeatedly send the same update under new ids.'],
    parameters: Type.Object({
      id: Type.String({ minLength: 1, maxLength: 128, pattern: '^[a-zA-Z0-9:_-]+$', description: 'Stable unique id for this logical reply; reuse on retry.' }),
      text: Type.String({ minLength: 1, maxLength: 12000, description: 'Concise reply, without secrets.' }),
      kind: Type.String({ enum: ['progress', 'question', 'result', 'error'], description: 'progress, question, result, or error' }),
      replyTo: Type.Optional(Type.String({ minLength: 1, maxLength: 128, description: 'Inbox message id being answered.' })),
    }, { additionalProperties: false }),
    async execute(_toolCallId, params, signal) {
      const result = await client.reply(params, signal);
      return { content: [{ type: 'text', text: formatToolOutput(result) }], details: {} };
    },
  });
  pi.on('session_start', async (_event, ctx) => {
    try {
      await client.register();
      if (ctx.hasUI) ctx.ui.setStatus('orchestrator', 'Orchestrator connected');
    } catch {
      if (ctx.hasUI) ctx.ui.notify('Orchestrator unavailable. Reply tools will retry registration when called.', 'warning');
    }
  });
}
