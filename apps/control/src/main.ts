import { loadConfig } from './config.js';
import { Store } from './store.js';
import { CodexAdapter } from './codex.js';
import { HerdrAdapter } from './herdr.js';
import { MockReasoner, MockWorkers } from './mock.js';
import { ControlService } from './service.js';
import { createHttpServer } from './http.js';

const config = loadConfig();
const store = new Store(config.db);
const service = new ControlService(config, store, config.mode === 'mock' ? new MockWorkers(store) : new HerdrAdapter(config), config.mode === 'mock' ? new MockReasoner() : new CodexAdapter(config, store));
const { server, closeStreams } = createHttpServer(service);
server.listen(config.port, config.host, () => {
  service.start();
  console.log(`Orchestrator control (${config.mode}) listening on ${config.host}:${config.port}`);
});
let stopping = false;
async function stop(): Promise<void> {
  if (stopping) return; stopping = true;
  closeStreams(); server.close(); server.closeAllConnections();
  await service.close(); store.close();
}
process.on('SIGINT', () => { void stop(); });
process.on('SIGTERM', () => { void stop(); });
server.on('error', error => { console.error(error.message); process.exitCode = 1; void stop(); });
