import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { execFileSync } from 'node:child_process';
import { join } from 'node:path';
import { HerdrAdapter, workerArguments } from '../src/herdr.js';
import { fixture } from './helpers.js';
import { now, UncertainError, UnsafeDeliveryError, type Session, type WorkerLaunch } from '../src/types.js';

function fakeHerdr(dir: string, mode: string) {
  const bin = join(dir, `herdr-${mode}`); const log = join(dir, `${mode}.jsonl`); const worktree = join(dir, 'dedicated'); mkdirSync(worktree, { recursive: true });
  const state = join(dir, 'started');
  writeFileSync(bin, `#!${process.execPath}\nimport {appendFileSync,existsSync,writeFileSync} from 'node:fs';
const args=process.argv.slice(2), mode=${JSON.stringify(mode)}, state=${JSON.stringify(state)};
appendFileSync(${JSON.stringify(log)},JSON.stringify(args)+'\\n');
const agent={agent:'pi',agent_session:{agent:'pi',kind:'id',value:mode==='replacement'||mode==='post-change'&&existsSync(state)?'other':'native-1',source:'hook'},pane_id:'pane-1',workspace_id:'workspace-1',name:'worker',cwd:${JSON.stringify(worktree)},agent_status:mode==='blocked'?'blocked':'idle',interactive_ready:true};
const emit=result=>console.log(JSON.stringify({result}));
if(args[0]==='worktree'){
 if(mode==='exit')process.exit(2);
 if(mode==='malformed'){console.log('invalid');process.exit(0);}
 emit({workspace:{workspace_id:'workspace-1'},root_pane:{pane_id:'pane-1',agent:mode==='hook'?'pi':null},worktree:{path:${JSON.stringify(worktree)}}});
}else if(args[1]==='list'){emit({agents:existsSync(state)||['replacement','blocked','idle','missing-id','post-change','ack-change','prompt-exit'].includes(mode)?[{...agent,agent_session:mode==='missing-id'?null:agent.agent_session}]:[]});}
else if(args[1]==='start'){writeFileSync(state,'yes');emit({agent});}
else if(args[1]==='prompt'){if(mode==='prompt-exit')process.exit(2);writeFileSync(state,'yes');emit({agent:mode==='ack-change'?{...agent,agent_session:{...agent.agent_session,value:'new-session'}}:agent});}
`, { mode: 0o700 });
  return { bin, log, worktree };
}
function session(): Session { return { id: 'worker-1', name: 'worker', agent: 'pi', cwd: '/unused', paneId: 'pane-1', workspaceId: 'workspace-1', status: 'idle', messaging: 'limited', createdAt: now(), updatedAt: now() }; }

test('Herdr worktree provisioning uses real response IDs, no-focus, native startup args and credential file paths', async () => {
  const f = fixture(); const fake = fakeHerdr(f.dir, 'create');
  execFileSync('git', ['init', '--quiet', f.dir]);
  const adapter = new HerdrAdapter({ ...f.config, herdrBin: fake.bin });
  const extension = join(f.dir, 'extension.ts'); writeFileSync(extension, '');
  const launch: WorkerLaunch = { credentialFile: join(f.dir, 'private token'), url: 'http://127.0.0.1:8787', command: '/worker/mcp', piExtension: extension };
  try {
    const native = await adapter.create('worker-1', { agent: 'pi', cwd: f.dir, name: 'name; $(touch should-not-exist)', instructions: 'task' }, launch);
    assert.equal(native.paneId, 'pane-1'); assert.equal(native.nativeId, 'pi:id:native-1'); assert.equal(native.workspaceId, 'workspace-1');
    const log = readFileSync(fake.log, 'utf8').trim().split('\n').map(line => JSON.parse(line) as string[]);
    assert.deepEqual(log[0]!.slice(0, 4), ['worktree', 'create', '--cwd', f.dir]);
    assert.ok(log[0]!.includes('--no-focus')); assert.ok(!log[0]!.includes('--focus'));
    assert.ok(log[0]!.includes('name; $(touch should-not-exist)'));
    const start = log.find(args => args[1] === 'start')!;
    assert.ok(start.includes('pane-1')); assert.ok(start.includes('--orchestrator-credential-file')); assert.ok(start.includes(launch.credentialFile));
    assert.ok(!start.some(arg => arg.includes('ORCHESTRATOR_WORKER_TOKEN')));
    assert.deepEqual(workerArguments('pi', launch), ['-e', extension, '--orchestrator-credential-file', launch.credentialFile, '--orchestrator-url', launch.url]);
    assert.match(workerArguments('codex', launch).join(' '), /mcp_servers.orchestrator.args=/);
    assert.ok(workerArguments('claude', launch).includes('--strict-mcp-config'));
  } finally { await f.close(); }
});

for (const mode of ['hook', 'exit', 'malformed']) test(`Herdr ${mode} during creation is uncertain and never receives shell injection`, async () => {
  const f = fixture(); const fake = fakeHerdr(f.dir, mode); execFileSync('git', ['init', '--quiet', f.dir]);
  const extension = join(f.dir, 'extension.ts'); writeFileSync(extension, '');
  const adapter = new HerdrAdapter({ ...f.config, herdrBin: fake.bin });
  try {
    await assert.rejects(adapter.create('id', { agent: 'pi', cwd: f.dir, name: 'test', instructions: 'task' }, { credentialFile: '/secret-file', url: 'http://localhost', command: '/mcp', piExtension: extension }), UncertainError);
    assert.ok(!readFileSync(fake.log, 'utf8').includes('"start"'));
  } finally { await f.close(); }
});

for (const mode of ['replacement', 'blocked', 'missing-id']) test(`Herdr ${mode} observation refuses prompt delivery`, async () => {
  const f = fixture(); const fake = fakeHerdr(f.dir, mode); const adapter = new HerdrAdapter({ ...f.config, herdrBin: fake.bin });
  try {
    await assert.rejects(adapter.send(session(), 'pi:id:native-1', 'do task'), UnsafeDeliveryError);
    assert.ok(!readFileSync(fake.log, 'utf8').includes('"prompt"'));
  } finally { await f.close(); }
});

test('Herdr prompt acknowledgement waits for state change and is not task completion', async () => {
  const f = fixture(); const fake = fakeHerdr(f.dir, 'idle'); const adapter = new HerdrAdapter({ ...f.config, herdrBin: fake.bin });
  try {
    await adapter.send(session(), 'pi:id:native-1', 'literal; $(no shell)');
    const log = readFileSync(fake.log, 'utf8').trim().split('\n').map(line => JSON.parse(line) as string[]);
    assert.equal(log.length, 3); const prompt = log[1]!;
    assert.deepEqual(prompt.slice(0, 4), ['agent', 'prompt', 'pane-1', 'literal; $(no shell)']);
    assert.ok(prompt.includes('--wait')); assert.ok(prompt.includes('--timeout'));
  } finally { await f.close(); }
});

for (const mode of ['post-change', 'ack-change', 'prompt-exit']) test(`Herdr ${mode} between preflight and acknowledgement is uncertain, never retried`, async () => {
  const f = fixture(); const fake = fakeHerdr(f.dir, mode); const adapter = new HerdrAdapter({ ...f.config, herdrBin: fake.bin });
  try {
    await assert.rejects(adapter.send(session(), 'pi:id:native-1', 'task'), UncertainError);
    const log = readFileSync(fake.log, 'utf8').trim().split('\n').map(line => JSON.parse(line) as string[]);
    assert.equal(log.filter(args => args[1] === 'prompt').length, 1);
  } finally { await f.close(); }
});
