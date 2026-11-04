import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { Outbox } from './outbox.js';

test('an enqueued source survives an actual abrupt process termination',
  { skip: process.platform === 'win32' }, async () => {
    const root = mkdtempSync(join(tmpdir(), 'goldfish-process-recovery-'));
    const directory = join(root, 'new', 'nested', 'outbox');
    try {
      const script = `
        import { Outbox } from ${JSON.stringify(new URL('./outbox.js', import.meta.url).href)};
        const queue = new Outbox(process.argv[1], async () => { throw Error('offline'); });
        queue.enqueue('/ingest', {session_id:'s', message_id:'s:1', event_seq:1,
          role:'user', source:'human', text:'synthetic original evidence'});
        process.kill(process.pid, 'SIGKILL');
      `;
      const child = spawnSync(process.execPath, ['--input-type=module', '-e', script, directory],
        { encoding: 'utf8' });
      assert.equal(child.signal, 'SIGKILL');
      assert.equal(readdirSync(directory).filter(name => name.endsWith('.tmp')).length, 0);
      const received = [];
      const restored = new Outbox(directory, async (path, payload) => {
        received.push({ path, payload });
        return { ok: true };
      });
      await restored.drain();
      await restored.close();
      assert.equal(received.length, 1);
      assert.equal(received[0].payload.text, 'synthetic original evidence');
      assert.equal(readdirSync(directory).length, 0);
    } finally { rmSync(root, { recursive: true, force: true }); }
  });
