import test from 'node:test';
import assert from 'node:assert/strict';
import { writeFileSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { CodexAdapter, codexProcessArgs } from '../src/codex.js';
import { fixture } from './helpers.js';
import { UncertainError } from '../src/types.js';

function fakeCodex(dir: string, mode = 'success'): { bin: string; log: string } {
  const bin = join(dir, `codex-${mode}`); const log = join(dir, `${mode}.jsonl`);
  writeFileSync(bin, `#!${process.execPath}\nimport {createInterface} from 'node:readline';
import {appendFileSync} from 'node:fs';
const mode=${JSON.stringify(mode)}, log=${JSON.stringify(log)};
const write = obj => process.stdout.write(JSON.stringify(obj)+'\\n');
appendFileSync(log,JSON.stringify({argv:process.argv.slice(2),speechKeyPresent:Boolean(process.env.OPENAI_API_KEY),elevenKeyPresent:Boolean(process.env.ELEVEN_API_KEY)})+'\\n');
let thread='thread-persisted', turn='turn-1', waiting=0, hostEnabled=false;
const finish=()=>{write({method:'item/agentMessage/delta',params:{threadId:thread,turnId:turn,itemId:'answer',delta:'partial'}});write({method:'item/completed',params:{threadId:thread,turnId:turn,item:{type:'agentMessage',id:'answer',text:'Final answer'}}});write({method:'turn/completed',params:{threadId:thread,turn:{id:turn,status:'completed',items:[],error:null}}});};
const streamAnswer=()=>{
 write({method:'item/started',params:{threadId:thread,turnId:'stale',item:{type:'agentMessage',id:'stale',phase:'final_answer',text:'WRONG'}}});
 write({method:'item/completed',params:{threadId:'other',turnId:turn,item:{type:'agentMessage',id:'wrong',text:'WRONG'}}});
 write({method:'item/completed',params:{threadId:thread,turnId:turn,item:{type:'reasoning',id:'reason',text:'PRIVATE REASONING'}}});
 write({method:'item/completed',params:{threadId:thread,turnId:turn,item:{type:'agentMessage',id:'progress',phase:'commentary',text:'I will check.'}}});
 write({method:'item/started',params:{threadId:thread,turnId:turn,item:{type:'agentMessage',id:'answer',phase:'final_answer',text:''}}});
 write({method:'item/agentMessage/delta',params:{threadId:thread,turnId:turn,itemId:'answer',delta:'First. '}});
 write({method:'item/agentMessage/delta',params:{threadId:thread,turnId:'stale',itemId:'answer',delta:'WRONG'}});
 write({method:'item/completed',params:{threadId:thread,turnId:turn,item:{type:'agentMessage',id:'answer',phase:'final_answer',text:'First. Second.'}}});
};
const streamDone=()=>write({method:'turn/completed',params:{threadId:thread,turn:{id:turn,status:'completed',items:[],error:null}}});
createInterface({input:process.stdin}).on('line',line=>{
 const m=JSON.parse(line);appendFileSync(log,JSON.stringify(m)+'\\n');
 const result=value=>write({id:m.id,result:value});
 if(m.method==='initialize')result({});
 else if(m.method==='initialized'){}
 else if(m.method==='config/read')result({config:{mcp_servers:{unsafe:{command:'sh',tool_timeout_sec:null},'computer-use':{url:'http://127.0.0.1:9999/mcp',command:null},'server.with.dots':{command:'false'}}}});
 else if(m.method==='thread/start'||m.method==='thread/resume'){
   const servers=m.params.config.mcp_servers;
   if(!servers || Object.values(servers).some(s=>s.enabled!==false || (!s.command && !s.url) || Object.values(s).some(v=>v===null)) || Object.keys(m.params.config).some(k=>k.startsWith('mcp_servers.'))){
     write({id:m.id,error:{code:-32600,message:'Invalid MCP override transport'}});return;
   }
   hostEnabled=m.params.config['features.code_mode_host']===true;
   result({thread:{id:thread}});
 }
 else if(m.method==='turn/start'){
   if(mode==='exit'){process.exit(3);return;}
   if(mode==='rpc-error'){write({id:m.id,error:{code:-1,message:'rejected'}});return;}
   if(mode==='early-stream'){streamAnswer();result({turn:{id:turn}});streamDone();return;}
   result({turn:{id:turn}});
   if(mode==='stream'||mode==='seal'){
     streamAnswer();
     if(mode==='seal'){waiting=1;write({id:'late',method:'item/tool/call',params:{threadId:thread,turnId:turn,callId:'late',tool:'create_session',arguments:{}}});}
     else streamDone();
     return;
   }
   if(mode==='mutating-stream'||mode==='reading-stream'){
     waiting=1;write({id:'initial',method:'item/tool/call',params:{threadId:thread,turnId:turn,callId:'initial',tool:mode==='mutating-stream'?'create_session':'list_sessions',arguments:{}}});
     if(mode==='reading-stream')streamAnswer();
     return;
   }
   if(mode==='timeout')return;
   if(mode==='oversized-final'){write({method:'turn/completed',params:{threadId:thread,turn:{id:turn,status:'completed',items:[{type:'agentMessage',id:'large',phase:'final_answer',text:'x'.repeat(32769)}]}}});return;}
   if(mode==='turn-error'){write({method:'turn/completed',params:{threadId:thread,turn:{id:turn,status:'failed',error:{message:'provider error'}}}});return;}
   if(mode==='notification-error'){write({method:'error',params:{threadId:thread,turnId:turn,willRetry:false,error:{message:'fatal'}}});return;}
   if(mode==='empty'){write({method:'turn/completed',params:{threadId:thread,turn:{id:turn,status:'completed',error:null}}});return;}
   if(mode==='malformed'){process.stdout.write('not json\\n');return;}
   if(mode==='tools' && hostEnabled){
     waiting=5;
     write({id:'tool-1',method:'item/tool/call',params:{threadId:thread,turnId:turn,callId:'stable-call',tool:'list_sessions',arguments:{}}});
     write({id:'approval',method:'item/commandExecution/requestApproval',params:{}});
     write({id:'file',method:'item/fileChange/requestApproval',params:{}});
     write({id:'permissions',method:'item/permissions/requestApproval',params:{}});
     write({id:'other',method:'exec-unrestricted',params:{}});
   }else finish();
 } else if(m.id && !m.method && --waiting===0){
   if(mode==='mutating-stream'){streamAnswer();streamDone();}
   else if(mode==='reading-stream'||mode==='seal')streamDone();
   else finish();
 }
});\n`, { mode: 0o700 });
  return { bin, log };
}
function records(log: string): Record<string, unknown>[] { return readFileSync(log, 'utf8').trim().split('\n').map(line => JSON.parse(line)); }

test('Codex JSONL initializes experimental routing tools, denies approvals, and resumes persisted thread', async () => {
  const f = fixture(); const fake = fakeCodex(f.dir, 'tools');
  const config = { ...f.config, codexBin: fake.bin, requestTimeoutMs: 5000, turnTimeoutMs: 3000 };
  let adapter = new CodexAdapter(config, f.store); let calls = 0;
  try {
    assert.equal(await adapter.turn('hi', 'op-one', async call => { calls++; assert.equal(call.callId, 'stable-call'); return { sessions: [] }; }), 'Final answer');
    assert.equal(calls, 1); assert.equal(adapter.status, 'ready'); assert.equal(f.store.metadata('codex.threadId'), 'thread-persisted');
    const log = records(fake.log);
    assert.deepEqual(log[0]!.argv, codexProcessArgs);
    const initialize = log.find(r => r.method === 'initialize')!;
    assert.match(JSON.stringify(initialize), /"experimentalApi":true/);
    const start = log.find(r => r.method === 'thread/start')!;
    const serialized = JSON.stringify(start);
    assert.match(serialized, /"type":"function"/); assert.match(serialized, /"sandbox":"read-only"/); assert.match(serialized, /"approvalPolicy":"never"/);
    assert.deepEqual((start.params as { config: { mcp_servers: unknown } }).config.mcp_servers, {
      unsafe: { command: 'sh', enabled: false },
      'computer-use': { url: 'http://127.0.0.1:9999/mcp', enabled: false },
      'server.with.dots': { command: 'false', enabled: false },
    });
    assert.equal((start.params as { config: Record<string, unknown> }).config['features.code_mode_host'], true);
    assert.equal(codexProcessArgs[codexProcessArgs.indexOf('code_mode_host') - 1], '--enable');
    for (const feature of ['shell_tool', 'unified_exec', 'code_mode', 'browser_use', 'computer_use']) {
      assert.equal(codexProcessArgs[codexProcessArgs.indexOf(feature) - 1], '--disable');
    }
    assert.deepEqual(log.find(r => r.id === 'approval')!.result, { decision: 'decline' });
    assert.deepEqual(log.find(r => r.id === 'file')!.result, { decision: 'decline' });
    assert.deepEqual(log.find(r => r.id === 'permissions')!.result, { permissions: {}, scope: 'turn' });
    assert.ok(log.find(r => r.id === 'other')!.error);
    assert.deepEqual(log.find(r => r.id === 'tool-1')!.result, { contentItems: [{ type: 'inputText', text: '{"sessions":[]}' }], success: true });
    adapter.close(); adapter = new CodexAdapter(config, f.store);
    assert.equal(await adapter.turn('again', 'op-two', async () => ({})), 'Final answer');
    assert.equal(records(fake.log).filter(r => r.method === 'thread/start').length, 1);
    assert.equal(records(fake.log).filter(r => r.method === 'thread/resume').length, 1);
  } finally { adapter.close(); await f.close(); }
});

for (const mode of ['exit', 'timeout', 'turn-error', 'notification-error', 'rpc-error', 'empty', 'malformed', 'oversized-final']) {
  test(`Codex ${mode} terminates pending turn without hanging or retrying`, async () => {
    const f = fixture(); const fake = fakeCodex(f.dir, mode);
    const adapter = new CodexAdapter({ ...f.config, codexBin: fake.bin, requestTimeoutMs: 5000, turnTimeoutMs: mode === 'timeout' ? 150 : 3000 }, f.store);
    try {
      await assert.rejects(adapter.turn('hi', 'one', async () => ({})), mode === 'exit' || mode === 'timeout' || mode === 'notification-error' || mode === 'malformed' || mode === 'oversized-final' ? UncertainError : Error);
      assert.equal(records(fake.log).filter(r => r.method === 'turn/start').length, 1);
    } finally { adapter.close(); await f.close(); }
  });
}

test('speech credentials are not inherited by the Codex reasoning subprocess', async () => {
  const f = fixture(); const fake = fakeCodex(f.dir);
  const previous = process.env.OPENAI_API_KEY;
  const previousEleven = process.env.ELEVEN_API_KEY;
  process.env.ELEVEN_API_KEY = "synthetic-eleven-only-test-key";
  process.env.OPENAI_API_KEY = 'synthetic-speech-only-test-key';
  const adapter = new CodexAdapter({ ...f.config, codexBin: fake.bin }, f.store);
  try {
    await adapter.turn('hi', 'speech-isolation', async () => ({}));
    assert.equal(records(fake.log)[0]!.speechKeyPresent, false);
    assert.equal(records(fake.log)[0]!.elevenKeyPresent, false);
  } finally {
    adapter.close();
    if (previous === undefined) delete process.env.OPENAI_API_KEY;
    else process.env.OPENAI_API_KEY = previous;
    if (previousEleven === undefined) delete process.env.ELEVEN_API_KEY;
    else process.env.ELEVEN_API_KEY = previousEleven;
    await f.close();
  }
});

test('Codex missing executable rejects pending initialize and closes cleanly', async () => {
  const f = fixture(); const adapter = new CodexAdapter({ ...f.config, codexBin: join(f.dir, 'missing') }, f.store);
  try { await assert.rejects(adapter.turn('hi', 'one', async () => ({})), UncertainError); }
  finally { adapter.close(); await f.close(); }
});


for (const mode of ['stream', 'early-stream', 'seal', 'mutating-stream', 'reading-stream']) {
  test(`Codex ${mode}: correlated final speech, commentary filtering and execution gate`, async () => {
    const f = fixture(); const fake = fakeCodex(f.dir, mode);
    const adapter = new CodexAdapter({ ...f.config, codexBin: fake.bin }, f.store);
    const chunks: string[] = []; let completed = false; let calls = 0; let readFinished = false;
    try {
      const answer = adapter.turn('hi', 'stream-one', async () => {
        calls++; await Promise.resolve(); readFinished = true; return { operationId: 'queued-child' };
      }, text => { assert.equal(completed, false); if (mode === 'reading-stream') assert.ok(readFinished); chunks.push(text); });
      assert.equal(await answer, 'First. Second.'); completed = true;
      assert.equal(chunks.join(''), mode === 'mutating-stream' ? '' : 'First. Second.');
      assert.equal(calls, ['mutating-stream', 'reading-stream'].includes(mode) ? 1 : 0);
      if (mode === 'seal') assert.equal((records(fake.log).find(r => r.id === 'late')!.result as {success: boolean}).success, false);
    } finally { adapter.close(); await f.close(); }
  });
}
