/** Local durable delivery queue. Files contain private dialogue; never log them. */
import { createHash, randomUUID } from 'node:crypto';
import {
  closeSync, fsyncSync, mkdirSync, openSync, readFileSync, readdirSync,
  renameSync, unlinkSync, writeFileSync,
} from 'node:fs';
import { dirname, join, resolve } from 'node:path';

function syncDirectory(directory) {
  // Windows cannot open directory handles with these standard Node APIs.
  // There the queue supports process restart recovery, not power-loss durability.
  if (process.platform === 'win32') return;
  const descriptor = openSync(directory, 'r');
  try { fsyncSync(descriptor); }
  finally { closeSync(descriptor); }
}

export class Outbox {
  constructor(directory, deliver, warn = () => {}) {
    this.directory = directory;
    this.deliver = deliver;
    this.warn = warn;
    this.running = null;
    this.closed = false;
    const firstCreated = mkdirSync(directory, { recursive: true, mode: 0o700 });
    // Node on Windows may return a \\?\-prefixed firstCreated path. Directory
    // fsync is unsupported there anyway; do not compare it against an ordinary
    // path while walking parents (the two roots can never compare equal).
    if (firstCreated && process.platform !== 'win32') {
      // Sync every newly created directory entry, including its existing parent.
      const parent = dirname(resolve(firstCreated));
      for (let current = resolve(directory); ; current = dirname(current)) {
        syncDirectory(current);
        if (current === parent) break;
      }
    }
  }

  enqueue(path, payload, { deferUntilSchedule = false } = {}) {
    if (this.closed) return;
    const identity = path + ':' + (payload.message_id ?? payload.session_id);
    const filename = join(this.directory, createHash('sha256').update(identity).digest('hex') + '.json');
    const temporary = filename + '.' + randomUUID() + '.tmp';
    let descriptor;
    try {
      descriptor = openSync(temporary, 'wx', 0o600);
      writeFileSync(descriptor, JSON.stringify({ path, payload, deferUntilSchedule, deliveryId: randomUUID() }));
      fsyncSync(descriptor);
      closeSync(descriptor);
      descriptor = undefined;
      renameSync(temporary, filename);
      // fsync(file) alone does not persist the new filename after a power loss.
      syncDirectory(this.directory);
    } catch (error) {
      if (descriptor !== undefined) closeSync(descriptor);
      try { unlinkSync(temporary); } catch {}
      throw error;
    }
  }

  drain() {
    if (this.closed) return Promise.resolve();
    if (this.running) return this.running;
    this.running = this._drain().catch(() => {
      this.warn('outbox unavailable; pending events retained for recovery');
    }).finally(() => { this.running = null; });
    return this.running;
  }

  async _drain() {
    // Ingest precedes scheduling even after a process restart. Per-session
    // chronological ordering is retained by the immutable DSH event sequence.
    const records = [];
    for (const filename of readdirSync(this.directory).filter(name => name.endsWith('.json'))) {
      try {
        const record = JSON.parse(readFileSync(join(this.directory, filename), 'utf8'));
        if (!['/ingest', '/schedule'].includes(record.path) || !record.payload?.session_id) throw Error();
        records.push({ filename, ...record });
      } catch {
        this.warn('outbox record unreadable; retained for recovery');
      }
    }
    // Persist partial dialogue immediately, but expose it to the consolidator
    // only through a completed turn's boundary. In particular, a schedule for
    // turn N must not release turn N+1 while an earlier HTTP call is pending.
    const boundaries = new Map(records.filter(record => record.path === '/schedule')
      .map(record => [String(record.payload.session_id), record.payload.event_seq ?? Number.MAX_SAFE_INTEGER]));
    const ready = records.filter(record => !record.deferUntilSchedule
      || record.payload.event_seq <= (boundaries.get(String(record.payload.session_id)) ?? -1));
    ready.sort((a, b) => {
      if (a.path !== b.path) return a.path === '/ingest' ? -1 : 1;
      return String(a.payload.session_id).localeCompare(String(b.payload.session_id))
        || (a.payload.event_seq ?? 0) - (b.payload.event_seq ?? 0);
    });
    for (const record of ready) {
      if (this.closed) break;
      try {
        const response = await this.deliver(record.path, record.payload);
        if (response?.ok !== true) throw Error();
        const filename = join(this.directory, record.filename);
        // A live enqueue may replace the same schedule while HTTP is pending.
        // Acknowledge only the exact durable record that was delivered.
        const current = JSON.parse(readFileSync(filename, 'utf8'));
        if (current.deliveryId === record.deliveryId) {
          unlinkSync(filename);
          syncDirectory(this.directory);
        }
      } catch {
        this.warn('backend delivery deferred; private event remains in local outbox');
        break;
      }
    }
  }

  async close() {
    this.closed = true;
    await this.running;
  }
}
