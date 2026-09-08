import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { WorkerClient } from './client';
import { loadConfig, parseArguments } from './config';
import { createWorkerServer } from './mcp';

async function main(): Promise<void> {
  const client = new WorkerClient(await loadConfig(parseArguments(process.argv.slice(2))));
  const server = createWorkerServer(client);
  await server.connect(new StdioServerTransport());
  // A temporary backend outage must not hide the tools from the harness.
  void client.register().catch(() => { process.stderr.write('Orchestrator registration unavailable; tools will retry registration when called.\n'); });
  let closing = false;
  const close = (): void => {
    if (closing) return;
    closing = true;
    void server.close().finally(() => { process.exitCode = 0; });
  };
  process.once('SIGTERM', close);
  process.once('SIGINT', close);
}

void main().catch(() => {
  // Do not print credential file content or remote response bodies to MCP stderr.
  process.stderr.write('Orchestrator worker tools could not start. Check --credential-file permissions, --url, and installed dependencies.\n');
  process.exitCode = 1;
});
