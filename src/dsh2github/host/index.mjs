import { createServer } from 'node:http';
import { randomBytes, timingSafeEqual, createHash } from 'node:crypto';
import { mkdir, writeFile, rename, readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { ApprovalGate } from './approval.mjs';

export const name = 'dsh2github-host';
export const inject = ['sessionController', 'sessionQuery', 'agents', 'llm', 'agentDefaultModel'];
const BODY_LIMIT = 1024 * 1024;

export function authorized(header, token) {
  const a = Buffer.from(header || '');
  const b = Buffer.from('Bearer ' + token);
  return a.length === b.length && timingSafeEqual(a, b);
}

async function bodyOf(req) {
  let size = 0;
  const parts = [];
  for await (const part of req) {
    size += part.length;
    if (size > BODY_LIMIT) throw new Error('Request too large');
    parts.push(part);
  }
  return parts.length ? JSON.parse(Buffer.concat(parts).toString('utf8')) : {};
}

function textBlocks(content) {
  return (Array.isArray(content) ? content : []).filter(x => x?.type === 'text').map(x => x.text).join('\n');
}

export async function apply(ctx, config = {}) {
  const root = resolve(process.env.DSH_HOME || join(homedir(), '.dsh'));
  const endpointPath = join(root, 'dsh2github', 'endpoint.json');
  const token = randomBytes(32).toString('hex');
  const gate = new ApprovalGate();
  const controller = ctx.sessionController;
  const disposeApproval = ctx.on('approval/request', (req, next) => {
    let parent = req.agent.session?.header?.parentSession;
    let rootSessionId = String(req.agent.id);
    const visited = new Set();
    while (parent && !visited.has(parent)) {
      visited.add(parent);
      if (gate.owns(parent)) { rootSessionId = parent; break; }
      parent = ctx.agents.get(parent)?.session?.header?.parentSession;
    }
    let argumentsText;
    if (req.callId && req.agent.session) {
      const session = req.agent.session;
      for (let seq = session.seq - 1; seq >= Math.max(0, session.seq - 200); seq--) {
        const event = session.eventAt(seq);
        const blocks = event?.type === 'assistant/message' ? event.data?.message?.content : [];
        const call = blocks?.find(x => x.type === 'tool-call' && x.id === req.callId);
        if (call) { argumentsText = String(call.arguments).slice(0, 8000); break; }
      }
    }
    return gate.ask({ ...req, bridgeRootSessionId: rootSessionId, bridgeArguments: argumentsText }, next);
  }, { prepend: true });
  // Heartbeat enrolls only sessions whose GitHub rooms were explicitly connected.
  // Without a live watcher, approval delegates to the existing local answerers.
  async function dispatch(path, body) {
    if (path === '/health') {
      const inputs = [JSON.stringify(ctx.agentDefaultModel.currentSelection())];
      for (const file of [join(root, 'cordis.patch.yml'), join(root, 'profiles', config.profile || 'desktop', 'cordis.patch.yml')]) {
        try { inputs.push(await readFile(file, 'utf8')); } catch (error) {
          if (error.code !== 'ENOENT') throw error;
          inputs.push('');
        }
      }
      return { protocol: 1, generation: gate.generation, hostPid: process.pid,
        version: '0.1.0', default: ctx.agentDefaultModel.currentSelection(),
        routeFingerprint: createHash('sha256').update(JSON.stringify(inputs)).digest('hex'),
        sessionModelSelection: typeof controller.agents?.selectForNextRequest === 'function' };
    }
    if (path === '/heartbeat') {
      if (!Array.isArray(body.sessions) || body.sessions.some(s => typeof s !== 'string')) throw new Error('Invalid sessions');
      gate.heartbeat(body.sessions);
      return { ok: true, generation: gate.generation };
    }
    if (path === '/sessions') {
      const listed = await controller.list({}, new AbortController().signal);
      const titles = await ctx.sessionQuery.readTitleSnapshots(listed.items.map(x => x.sessionId));
      const titleMap = new Map(titles.map(x => [x.sessionId, x.status === 'fulfilled' ? x.value?.title?.title : undefined]));
      return { items: listed.items.map(row => ({ ...row, title: titleMap.get(row.sessionId) || row.sessionId })) };
    }
    if (path === '/models') return controller.modelCatalog();
    if (path === '/create') return controller.create({ sessionId: body.sessionId, cwd: body.cwd });
    if (path === '/prompt') {
      if (!['queue', 'steer'].includes(body.mode)) throw new Error('Invalid delivery mode');
      if (body.selection && body.mode !== 'steer') {
        // Native selectModel also saves the global default. Use the inspected
        // Session-local controller seam, and fail closed if its version changes.
        const manager = controller.agents;
        if (typeof manager?.selectForNextRequest !== 'function') throw new Error('DSH Session-local model adapter is unavailable');
        const found = await controller.resolveAgent(body.sessionId);
        if (found.error) throw found.error;
        const models = await ctx.llm.listModels(body.selection.provider);
        if (!models.some(x => x.id === body.selection.model)) throw new Error('Model unavailable');
        const resolved = await ctx.llm.resolveCallConfig(body.selection);
        const selection = { provider: resolved.provider, model: resolved.model,
          ...(resolved.reasoningEffort === undefined ? {} : { reasoningEffort: resolved.reasoningEffort }) };
        manager.selectForNextRequest(found.agent, selection);
      }
      return controller.prompt({ sessionId: body.sessionId, requestId: body.requestId,
        mode: body.mode, content: [{ type: 'text', text: body.text }], clientTimeZone: body.clientTimeZone || 'UTC' },
        new AbortController().signal);
    }
    if (path === '/session') {
      const snapshot = await ctx.sessionQuery.readSession(body.sessionId);
      const agent = ctx.agents.get(body.sessionId);
      const latestPolicy = [...snapshot.events].reverse().find(e => e.type === 'approval/policy');
      const latestPreset = [...snapshot.events].reverse().find(e => e.type === 'permission/preset');
      // Only user text, assistant text, and lifecycle metadata cross this API.
      // Tools, reasoning, system prompts, credentials and raw config stay local.
      const events = snapshot.events.filter(e => ['user/message', 'assistant/message', 'turn/start', 'turn/end'].includes(e.type)
          && (e.type !== 'user/message' || e.data?.source?.kind === 'user'))
        .map(e => ({ seq: e.seq, type: e.type, time: e.time, turn: e.data?.turn,
          ...(e.type === 'user/message' ? { text: textBlocks(e.data?.content), requestId: e.data?.source?.rpcId } : {}),
          ...(e.type === 'assistant/message' ? { text: textBlocks(e.data?.message?.content) } : {}),
          ...(e.type === 'turn/end' ? { reason: e.data?.reason } : {}) }));
      return { sessionId: body.sessionId, cwd: snapshot.session?.cwd || agent?.session?.header?.cwd,
        approvalPolicy: latestPolicy?.data?.policy || 'unknown',
        permissionPreset: latestPreset?.data?.preset || latestPreset?.data?.id || 'unknown',
        running: agent?.status === 'running', events,
        queue: agent ? [...agent.inbox.nextTurn, ...agent.inbox.nextStep].map(x => ({ id: x.id, text: textBlocks(x.content) })) : [] };
    }
    if (path === '/queue/remove') return controller.updateQueue({ sessionId: body.sessionId, itemId: body.itemId, action: { kind: 'remove' } });
    if (path === '/approvals') return { generation: gate.generation, items: gate.list() };
    if (path === '/approval/decide') return gate.decide(body);
    throw new Error('Unknown operation');
  }
  const server = createServer(async (req, res) => {
    res.setHeader('Content-Type', 'application/json; charset=utf-8');
    res.setHeader('Cache-Control', 'no-store');
    // Browser pages cannot access the Host token, and cross-origin requests
    // are rejected even if a caller attempts a preflight or forged body.
    if (req.headers.origin || !authorized(req.headers.authorization, token)) {
      res.writeHead(403); res.end(JSON.stringify({ error: 'Unauthorized' })); return;
    }
    if (req.method !== 'POST') { res.writeHead(405); res.end('{}'); return; }
    try {
      const value = await dispatch(req.url, await bodyOf(req));
      res.end(JSON.stringify(value));
    } catch (error) {
      res.writeHead(409);
      res.end(JSON.stringify({ error: error.message || 'Host operation failed' }));
    }
  });
  await new Promise((resolve, reject) => { server.once('error', reject); server.listen(0, '127.0.0.1', resolve); });
  await mkdir(join(root, 'dsh2github'), { recursive: true });
  const endpoint = { protocol: 1, host: '127.0.0.1', port: server.address().port, token, pid: process.pid, generation: gate.generation };
  await writeFile(endpointPath + '.tmp', JSON.stringify(endpoint), { mode: 0o600 });
  await rename(endpointPath + '.tmp', endpointPath);
  ctx.effect(() => () => {
    gate.close(); disposeApproval(); server.close();
    // A stopped endpoint is harmless. Deleting it during asynchronous disposal
    // could erase discovery already written by a newly mounted generation.
  }, 'dsh2github: local authenticated Host');
}
