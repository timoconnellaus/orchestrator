import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
import { WorkerClient } from './client';
import { loadConfig, parseArguments } from './config';
import { createWorkerServer } from './mcp';

async function main(): Promise<void> {
  const shutdown = new AbortController();
  let server: ReturnType<typeof createWorkerServer> | undefined;
  let closing = false;
  const close = (): void => {
    if (closing) return;
    closing = true;
    shutdown.abort();
    process.stdin.pause();
    // Protocol.close aborts in-flight MCP request signals as well as the transport.
    void server?.close().catch(() => { process.exitCode = 1; });
  };
  // Attach before configuration I/O: a harness can close stdin during startup.
  process.stdin.once('end', close);
  process.stdin.once('error', close);
  process.once('SIGTERM', close);
  process.once('SIGINT', close);
  const client = new WorkerClient(await loadConfig(parseArguments(process.argv.slice(2))));
  if (closing) return;
  server = createWorkerServer(client);
  server.onclose = close;
  await server.connect(new StdioServerTransport());
  if (closing) return;
  // A temporary backend outage must not hide the tools from the harness.
  void client.register(shutdown.signal).catch(() => {
    if (!closing) process.stderr.write('Orchestrator registration unavailable; tools will retry registration when called.\n');
  });
}

void main().catch(() => {
  // Do not print credential file content or remote response bodies to MCP stderr.
  process.stderr.write('Orchestrator worker tools could not start. Check --credential-file permissions, --url, and installed dependencies.\n');
  process.exitCode = 1;
});
