import assert from 'node:assert/strict';
import test from 'node:test';
import { chmod, mkdtemp, rm, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadConfig, normalizeUrl, parseArguments } from '../src/config';

const token = 'test-session-credential-not-real';

test('command arguments take precedence and have no raw token flag', async () => {
  const args = parseArguments(['--url', 'http://localhost:9000/', '--credential-file', '/private/worker']);
  assert.deepEqual(args, { url: 'http://localhost:9000/', credentialFile: '/private/worker' });
  assert.throws(() => parseArguments(['--token', token]));
  assert.throws(() => parseArguments(['--url']));
  assert.throws(() => parseArguments(['--url', '--credential-file']));
  assert.equal((await loadConfig({ url: 'http://localhost:9000', token }, { ORCHESTRATOR_URL: 'http://localhost:8000' })).url, 'http://localhost:9000');
});

test('URL preserves path prefixes but rejects embedded credentials/query/fragment', () => {
  assert.equal(normalizeUrl('http://localhost:8787/control/'), 'http://localhost:8787/control');
  for (const value of ['file:///tmp/x', 'http://user:secret@localhost', 'http://localhost?q=x', 'http://localhost/#x', 'not a url']) assert.throws(() => normalizeUrl(value));
});

test('credential file is required to be owner-only, bounded, regular, and not a symlink', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'orchestrator-worker-'));
  const path = join(dir, 'credential');
  try {
    await writeFile(path, token, { mode: 0o600 });
    assert.equal((await loadConfig({ credentialFile: path }, {})).token, token);
    await writeFile(path, JSON.stringify({ token, url: 'http://127.0.0.1:9999' }));
    assert.equal((await loadConfig({ credentialFile: path }, {})).url, 'http://127.0.0.1:9999');
    await chmod(path, 0o644);
    await assert.rejects(loadConfig({ credentialFile: path }, {}), /mode 0600/);
    await chmod(path, 0o600);
    const link = join(dir, 'link');
    await symlink(path, link);
    await assert.rejects(loadConfig({ credentialFile: link }, {}));
    await writeFile(path, 'x'.repeat(9000));
    await assert.rejects(loadConfig({ credentialFile: path }, {}), /at most 8 KiB/);
    await writeFile(path, '{"token":123}');
    await assert.rejects(loadConfig({ credentialFile: path }, {}), /token string/);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('unset or malformed credentials fail without revealing contents', async () => {
  for (const value of [undefined, '', 'short', 'has whitespace but long enough']) {
    await assert.rejects(loadConfig({ token: value }, {}), /valid scoped worker credential/);
  }
});
