import test from 'node:test';
import assert from 'node:assert/strict';
import { TurnSpeech } from '../src/turn-speech.js';
const item = (id: string, text: string, phase: string | null = 'final_answer') => ({ type: 'agentMessage', id, text, phase });

test('TurnSpeech mutes commentary/unknown/reasoning, holds legacy until complete', () => {
  const chunks: string[] = []; const speech = new TurnSpeech(t => chunks.push(t));
  speech.item(item('progress', 'Checking.', 'commentary'), true);
  speech.item({ type: 'reasoning', id: 'r', text: 'Reasoning' }, true);
  speech.item(item('unknown', 'Unknown', 'future_phase'), true);
  speech.delta('missing', 'Unidentified');
  speech.item(item('legacy', '', null), false); speech.delta('legacy', 'Legacy');
  assert.deepEqual(chunks, []); assert.equal(speech.answer(), '');
  speech.item(item('legacy', 'Legacy final.', null), true);
  assert.equal(speech.answer(), 'Legacy final.'); assert.deepEqual(chunks, []);
});

test('TurnSpeech waits for reads, seals, suppresses completion duplicate, and detects correction', () => {
  const chunks: string[] = []; const speech = new TurnSpeech(t => chunks.push(t));
  speech.inFlight = 1;
  speech.item(item('a', ''), false); speech.delta('a', 'First. ');
  assert.deepEqual(chunks, []); assert.equal(speech.sealed, false);
  speech.inFlight = 0; speech.flush(); assert.equal(speech.sealed, true);
  speech.item(item('a', 'Corrected.'), true); speech.item(item('a', 'Corrected.'), true);
  assert.deepEqual(chunks, ['First. ']); assert.equal(speech.answer(), 'Corrected.');
});

test('TurnSpeech mutation disables early output and final output excludes commentary', () => {
  const speech = new TurnSpeech(() => assert.fail('No early speech'));
  speech.mutated = true; speech.item(item('p', 'Checking.', 'commentary'), true);
  speech.item(item('a', 'Queued; not finished.'), true);
  assert.equal(speech.answer(), 'Queued; not finished.'); assert.equal(speech.sealed, false);
});

test('TurnSpeech observer exceptions do not fail work; buffers are bounded', () => {
  const speech = new TurnSpeech(() => { throw new Error('consumer disconnected'); });
  speech.item(item('a', 'Answer.'), true); assert.equal(speech.answer(), 'Answer.');
  const bounded = new TurnSpeech(); bounded.item(item('a', ''), false);
  assert.throws(() => bounded.delta('a', 'x'.repeat(32769)), /limit/);
});

test('TurnSpeech terminal snapshot correction remains canonical; closed turns never publish again', () => {
  const chunks: string[] = []; const speech = new TurnSpeech(t => chunks.push(t));
  speech.item(item('a', 'First.'), true); speech.end();
  speech.item(item('a', 'Corrected final.'), true);
  assert.deepEqual(chunks, ['First.']); assert.equal(speech.answer(), 'Corrected final.');
  assert.throws(() => speech.item(item('b', 'x'.repeat(32769)), true), /limit/);
});
