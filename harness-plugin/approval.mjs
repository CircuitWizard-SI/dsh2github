import { randomUUID } from 'node:crypto';

/** A decision belongs to one exact, live request, session, and Host generation. */
export class ApprovalGate {
  constructor({ now = Date.now, leaseMs = 60000, timeoutMs = 900000 } = {}) {
    this.now = now;
    this.leaseMs = leaseMs;
    this.timeoutMs = timeoutMs;
    this.generation = randomUUID();
    this.leaseUntil = 0;
    this.sessions = new Set();
    this.pending = new Map();
    this.receipts = new Map();
  }

  heartbeat(sessions) {
    this.sessions = new Set(sessions);
    this.leaseUntil = this.now() + this.leaseMs;
  }

  owns(sessionId) {
    return this.leaseUntil > this.now() && this.sessions.has(sessionId);
  }

  async ask(request, next) {
    const sessionId = String(request.agent.id);
    const rootSessionId = request.bridgeRootSessionId || sessionId;
    if (!this.owns(rootSessionId) || request.signal?.aborted) return next();
    const id = randomUUID();
    let finish;
    const promise = new Promise(resolve => { finish = resolve; });
    const row = { id, sessionId, rootSessionId, generation: this.generation, toolName: request.toolName,
      callId: request.callId, reason: request.reason || '', createdAt: this.now(),
      arguments: request.bridgeArguments, expiresAt: this.now() + this.timeoutMs, finish };
    this.pending.set(id, row);
    const abort = () => finish('cancelled');
    request.signal?.addEventListener('abort', abort, { once: true });
    if (request.signal?.aborted) abort();
    const timer = setInterval(() => {
      if (this.now() >= row.expiresAt) finish('rejected');
      else if (!this.owns(rootSessionId)) finish('delegate');
    }, 250);
    timer.unref?.();
    try {
      const result = await promise;
      this.pending.delete(id);
      return result === 'delegate' ? next() : result;
    } finally {
      clearInterval(timer);
      request.signal?.removeEventListener('abort', abort);
      this.pending.delete(id);
    }
  }

  list() {
    return [...this.pending.values()].map(({ finish, ...row }) => row);
  }

  decide({ id, sessionId, generation, decision, commentId }) {
    if (!['allowed-once', 'rejected'].includes(decision)) throw new Error('Only allow once or reject is supported');
    const previous = this.receipts.get(id);
    if (previous) {
      if (previous.sessionId !== sessionId || previous.generation !== generation ||
          previous.decision !== decision || previous.commentId !== commentId) throw new Error('Approval already decided');
      return previous;
    }
    const row = this.pending.get(id);
    if (!row || row.sessionId !== sessionId || row.generation !== generation ||
        !this.owns(row.rootSessionId) || this.now() >= row.expiresAt) throw new Error('Approval is stale or belongs to another session');
    const receipt = { id, sessionId, generation, decision, commentId, accepted: true };
    this.receipts.set(id, receipt);
    if (this.receipts.size > 2000) this.receipts.delete(this.receipts.keys().next().value);
    this.pending.delete(id);
    row.finish(decision);
    return receipt;
  }

  close() {
    for (const row of this.pending.values()) row.finish('cancelled');
    this.pending.clear();
    this.leaseUntil = 0;
  }
}
