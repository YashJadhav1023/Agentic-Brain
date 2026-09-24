// office.js — the "Office" tab of Mission Control: a pixel-art floor where
// every agent/account has a desk and every task is a paper card that travels
// Inbox -> Router -> desk -> Done shelf / Escalations corner.
//
// Data: GET /api/office/state (Bearer token via the page's fetchWithAuth).
// When that endpoint is missing (404/501), unreachable, or reports nothing at
// all, the floor runs a clearly labelled client-side demo simulation built to
// the same contract, so a fresh install still shows how the pipeline works.
//
// Credits: several scene ideas are adapted from munder-difflin by Chaitanya
// Giri (MIT licence; see THIRD_PARTY_NOTICES.md): avatars seated at desks that
// animate while their agent works, a desk device that "comes alive" while its
// owner types, a cream thought cloud with trailing puffs above a busy avatar, a
// red "!" over a blocked one, papers/envelopes flying along an eased arc with
// an arrival burst, and a stable first-free seat pool. This is an independent
// canvas-2D implementation: no code, art or assets were copied. Every sprite is
// drawn procedurally from the palettes below; none of that project's bundled
// tilesets (third-party, not redistributable) or TV-show characters are used.
//
// Security: every string in the state is untrusted. Canvas text is inert, and
// anything that reaches HTML goes through ui.esc() or textContent.
(function () {
  'use strict';

  const ui = window.ui;
  const esc = ui.esc;

  const API_URL = '/api/office/state';
  const POLL_MS = 3000;        // live poll cadence while the tab is visible
  const TICK_MS = 1500;        // scheduler granularity (and demo step)
  const PROBE_MS = 60000;      // re-check a missing endpoint this often
  const FRAME_MS = 1000 / 30;  // draw at most ~30 fps
  const NARROW_PX = 600;       // below this stage width use the portrait floor
  const MAX_AGENTS = 24;
  const SANS = '"Plus Jakarta Sans", system-ui, sans-serif';
  const MONO = '"JetBrains Mono", ui-monospace, monospace';

  const STATUSES = ['idle', 'working', 'blocked', 'offline'];
  const STAGES = ['queued', 'routing', 'running', 'review', 'done', 'escalated', 'delivered'];
  const FLOW_TYPES = ['dispatch', 'route', 'start', 'finish', 'escalate', 'failover', 'memory_write', 'handoff', 'deliver'];

  // ── Agent kinds: original procedural avatars, one look per kind ─────────
  // shirt/shade/hair/skin feed the sprite template; `color` tints the desk
  // device logo, the card edge and the label dot; `prop` names the accessory.
  const KINDS = {
    'kiro-cli':        { name: 'Kiro CLI', shirt: '#8B5CF6', shade: '#6D28D9', hair: '#3F2A1E', skin: '#F2C9A0', color: '#A78BFA', prop: 'headset' },
    'cline':           { name: 'Cline', shirt: '#14B8A6', shade: '#0F766E', hair: '#1F2937', skin: '#C68A5E', color: '#2DD4BF', prop: 'cap' },
    'antigravity':     { name: 'Antigravity', shirt: '#3B82F6', shade: '#1D4ED8', hair: '#E5E7EB', skin: '#E8B48A', color: '#60A5FA', prop: 'orbit' },
    'antigravity-api': { name: 'Antigravity (API key)', shirt: '#6366F1', shade: '#4338CA', hair: '#111827', skin: '#8D5B3E', color: '#818CF8', prop: 'keycard' },
    'antigravity-ide': { name: 'Antigravity IDE', shirt: '#F59E0B', shade: '#B45309', hair: '#B91C1C', skin: '#F5D0B5', color: '#FBBF24', prop: 'pencil' },
    'openhands':       { name: 'OpenHands', shirt: '#F97316', shade: '#C2410C', hair: '#FCD34D', skin: '#D9A07A', color: '#FB923C', prop: 'gloves' },
    'api':             { name: 'API worker', shirt: '#64748B', shade: '#475569', hair: '#CBD5E1', skin: '#CBD5E1', color: '#94A3B8', prop: 'robot' },
    'other':           { name: 'Agent', shirt: '#22C55E', shade: '#15803D', hair: '#57534E', skin: '#E0AC82', color: '#4ADE80', prop: 'none' },
  };
  const KIND_ORDER = Object.keys(KINDS);
  const TIER_COLORS = { frontier: '#C084FC', advanced: '#60A5FA', balanced: '#22D3EE', fast: '#4ADE80', coding: '#2DD4BF', auto: '#A1A1AA' };
  const STATUS_COLORS = { idle: '#A1A1AA', working: '#06B6D4', blocked: '#F59E0B', offline: '#EF4444' };
  const STAGE_EDGE = { queued: '#06B6D4', routing: '#A78BFA', review: '#F59E0B', done: '#22C55E', escalated: '#EF4444', delivered: '#FBBF24' };
  // Stage -> a ui-kit status word, so badges share the dashboard's tones.
  const STAGE_BADGE = { queued: 'queued', routing: 'running', running: 'running', review: 'paused', done: 'done', escalated: 'escalated', delivered: 'pending' };

  const C = {
    floorA: '#1A1E26', floorB: '#1C2029', floorLine: '#161920',
    wall: '#2A303C', wallTop: '#343B4A', baseboard: '#11141A',
    sky: '#0E1A2E', frame: '#4B5563', star: '#CBD5E1',
    wood: '#8A6848', woodLight: '#A5825D', woodDark: '#5E4631', woodShadow: '#3B2C1F',
    metal: '#6B7280', metalLight: '#9CA3AF', metalDark: '#374151',
    paper: '#F5F1E6', paperLine: '#C9C2B0', ink: '#1C1917',
    chair: '#2B3040', chairLight: '#3A4155',
    rug: '#232A36', rugEdge: '#2D3544',
    esc: '#3A1F22', escDark: '#2A1618', hazard: '#F59E0B',
    cork: '#9A7650', corkDark: '#7C5D3D', boardFrame: '#4A3726',
    lid: '#3F3F46', lidLight: '#52525B', keys: '#71717A',
    select: '#FDE047', note: '#FDE68A', bad: '#EF4444', ok: '#22C55E',
  };

  // ── Small helpers ─────────────────────────────────────────────────────────
  function str(v, max) {
    if (v === null || v === undefined) return null;
    let s = typeof v === 'string' ? v : (typeof v === 'number' || typeof v === 'boolean' ? String(v) : '');
    s = s.replace(/[\u0000-\u001f\u007f]+/g, ' ').trim();
    if (!s) return null;
    return s.length > max ? s.slice(0, max - 1) + '…' : s;
  }
  function num(v) { const n = typeof v === 'number' ? v : Number(v); return v === null || v === undefined || v === '' || !Number.isFinite(n) ? null : n; }
  function arr(v) { return Array.isArray(v) ? v : []; }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }
  function ease(t) { return t < 0.5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2; }
  function isoTime(v) { const t = Date.parse(v || ''); return Number.isNaN(t) ? null : t; }
  function kindOf(k) { return KINDS[k] ? k : 'other'; }
  function nowS() { return performance.now() / 1000; }
  function hash(s) { let h = 2166136261; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 16777619); } return h >>> 0; }

  // ── Contract normalisation (defensive: extra fields ignored, bad ones dropped)
  function normalize(raw) {
    const r = raw && typeof raw === 'object' ? raw : {};
    const src = r.sources && typeof r.sources === 'object' ? r.sources : {};
    const seenA = new Set();
    const agents = [];
    for (const a of arr(r.agents)) {
      if (!a || typeof a !== 'object') continue;
      const id = str(a.id, 80);
      if (!id || seenA.has(id)) continue;
      seenA.add(id);
      agents.push({
        id, label: str(a.label, 60) || id, kind: kindOf(a.kind), account: str(a.account, 80),
        status: STATUSES.includes(a.status) ? a.status : 'idle',
        current_task_id: str(a.current_task_id, 120), model: str(a.model, 80), source: str(a.source, 30),
      });
      if (agents.length >= 64) break;
    }
    const seenT = new Set();
    const tasks = [];
    for (const t of arr(r.tasks)) {
      if (!t || typeof t !== 'object') continue;
      const id = str(t.id, 120);
      if (!id || seenT.has(id)) continue;
      seenT.add(id);
      const conf = num(t.confidence);
      tasks.push({
        id, title: str(t.title, 140) || id, stage: STAGES.includes(t.stage) ? t.stage : 'queued',
        agent: str(t.agent, 80), kind: t.kind ? kindOf(t.kind) : null, model: str(t.model, 80), model_tier: str(t.model_tier, 30),
        complexity: str(t.complexity, 30), risk: str(t.risk, 30), routing_reason: str(t.routing_reason, 300),
        confidence: conf === null ? null : clamp(conf, 0, 1), requires_approval: t.requires_approval === true,
        created_at: str(t.created_at, 40), started_at: str(t.started_at, 40), completed_at: str(t.completed_at, 40),
        duration_s: num(t.duration_s), sandbox_branch: str(t.sandbox_branch, 160),
        attempts: arr(t.attempts).map((x) => str(x, 160)).filter(Boolean).slice(0, 12), source: str(t.source, 30),
      });
      if (tasks.length >= 60) break;
    }
    const flows = [];
    for (const f of arr(r.flows)) {
      if (!f || typeof f !== 'object' || !FLOW_TYPES.includes(f.type)) continue;
      flows.push({ ts: str(f.ts, 40), type: f.type, task_id: str(f.task_id, 120), from: str(f.from, 80), to: str(f.to, 80), detail: str(f.detail, 160) });
      if (flows.length >= 100) break;
    }
    const mem = r.memory && typeof r.memory === 'object' ? r.memory : {};
    const ho = r.handoff && typeof r.handoff === 'object' ? r.handoff : {};
    return {
      generated_at: str(r.generated_at, 40),
      sources: { mission_control: src.mission_control === true, brain_swarm: src.brain_swarm === true, brain_dir: str(src.brain_dir, 200) },
      agents, tasks, flows,
      memory: {
        entries: Math.max(0, Math.round(num(mem.entries) || 0)),
        recent: arr(mem.recent).slice(0, 20).filter((m) => m && typeof m === 'object')
          .map((m) => ({ ts: str(m.ts, 40), agent: str(m.agent, 80), scope: str(m.scope, 60) })),
      },
      handoff: { title: str(ho.title, 160), status: str(ho.status, 30), agent: str(ho.agent, 60), next: str(ho.next, 240), updated_at: str(ho.updated_at, 40) },
    };
  }

  // "Sources are empty": nothing to put on the floor at all.
  function isEmptyState(st) {
    return (!st.sources.mission_control && !st.sources.brain_swarm) || (!st.agents.length && !st.tasks.length && !st.flows.length);
  }

  // ── Scene state ───────────────────────────────────────────────────────────
  const S = {
    els: null, active: false, started: false,
    mode: 'loading', demoReason: '', demo: null, fetchNote: '',
    state: null, lastFetchAt: 0, lastOkAt: 0, probeAfter: 0, inFlight: false, timer: 0, eventTimer: 0,
    seats: new Map(), desks: [], deskById: new Map(), layout: null, layoutKey: '',
    cards: new Map(), flyers: [], seenFlows: new Set(), decision: null,
    selection: null, hits: [], panelHtml: '', srSig: '', hotSig: '', statsSig: '',
    canvas: null, ctx: null, world: null, wctx: null, bg: null, bgKey: '',
    cssW: 0, cssH: 0, dpr: 1, scale: 1,
    raf: 0, lastDraw: 0, motionPaused: false, reducedMQ: null,
    routerBlinkUntil: 0, cabinetOpenUntil: 0, boardFlashUntil: 0,
    stats: { frames: 0, fetches: 0, snapshots: 0 },
    fitCache: new Map(),
  };

  function motionOn() { return !S.motionPaused && !(S.reducedMQ && S.reducedMQ.matches); }

  // ── Layout: world coordinates in "pixels" of the low-res floor ───────────
  function computeLayout(seatCount, narrow) {
    if (!narrow) {
      const cols = 4, modW = 64, modH = 76, top = 44;
      const rows = Math.max(2, Math.ceil(seatCount / cols));
      const H = Math.max(254, top + rows * modH + 10);
      return {
        W: 480, H, narrow, cols, rows, modW, modH, wallH: 36,
        door: { x: 12, y: 6, w: 22, h: 30 },
        board: { x: 188, y: 3, w: 184, h: 30 },
        inbox: { x: 10, y: 46, w: 90, h: 54 },
        router: { x: 8, y: 112, w: 94, h: 78 },
        desks: { x: 108, y: top, w: cols * modW },
        done: { x: 378, y: 46, w: 94, h: 64 },
        esc: { x: 378, y: 120, w: 94, h: 62 },
        memory: { x: 380, y: 192, w: 44, h: 56 },
        plants: [{ x: 440, y: H - 20 }, { x: 14, y: H - 20 }, { x: 170, y: 38 }],
      };
    }
    const cols = 3, modW = 80, modH = 82, top = 124;
    const rows = Math.max(1, Math.ceil(seatCount / cols));
    const after = top + rows * modH + 2;
    return {
      W: 256, H: after + 66, narrow, cols, rows, modW, modH, wallH: 36,
      door: { x: 6, y: 6, w: 20, h: 30 },
      board: { x: 34, y: 3, w: 216, h: 30 },
      inbox: { x: 4, y: 44, w: 78, h: 72 },
      router: { x: 86, y: 42, w: 84, h: 78 },
      desks: { x: 8, y: top, w: cols * modW },
      done: { x: 174, y: 44, w: 78, h: 72 },
      esc: { x: 6, y: after, w: 132, h: 60 },
      memory: { x: 196, y: after + 2, w: 44, h: 56 },
      plants: [{ x: 150, y: after + 36 }, { x: 246, y: after + 40 }],
    };
  }

  // Clickable/focusable area of a desk: avatar, chair and desk surface.
  function agentRect(d) { return { x: d.x + 3, y: d.top - 24, w: d.w - 6, h: 44 }; }

  function deskRect(seat) {
    const L = S.layout;
    const col = seat % L.cols, row = Math.floor(seat / L.cols);
    const x = L.desks.x + col * L.modW, y = L.desks.y + row * L.modH;
    // cx: centre column; top: y of the desk surface (the avatar sits behind it)
    return { x, y, w: L.modW, h: L.modH, cx: x + Math.floor(L.modW / 2), top: y + L.modH - 41 };
  }

  // ── Station anchor points (where cards rest / fly to) ────────────────────
  function stationSlots(key) {
    const L = S.layout;
    if (key === 'inbox') {
      const r = L.inbox, cols = Math.floor((r.w - 16) / 8);
      return { cols, max: cols * 3, at: (i) => ({ x: r.x + 9 + (i % cols) * 8, y: r.y + r.h - 20 - Math.floor(i / cols) * 3 }) };
    }
    if (key === 'done') {
      const r = L.done, cols = Math.floor((r.w - 14) / 8), planks = shelfPlanks(r);
      return { cols, max: cols * planks.length, at: (i) => ({ x: r.x + 8 + (i % cols) * 8, y: planks[Math.floor(i / cols) % planks.length] - 5 }) };
    }
    if (key === 'esc') {
      const r = L.esc, cols = Math.max(3, Math.floor((r.w - 40) / 8));
      return { cols, max: cols * 3, at: (i) => ({ x: r.x + 26 + (i % cols) * 8, y: r.y + r.h - 16 - Math.floor(i / cols) * 3 }) };
    }
    if (key === 'router') {
      const r = L.router;
      return { cols: 4, max: 4, at: (i) => ({ x: r.x + Math.floor(r.w / 2) + 16 + (i % 2) * 2, y: r.y + 46 - i * 2 }) };
    }
    return null;
  }
  function shelfPlanks(r) { return [r.y + 26, r.y + 40, r.y + 54].filter((y) => y < r.y + r.h - 2); }
  function deskSlot(desk, i) { return { x: desk.cx + 12 + (i % 2) * 2, y: desk.top - 1 - i * 2 }; }
  function doorPt() { const d = S.layout.door; return { x: d.x + Math.floor(d.w / 2) - 3, y: d.y + d.h + 2 }; }
  function routerPt() { const r = S.layout.router; return { x: r.x + Math.floor(r.w / 2) - 3, y: r.y + 30 }; }
  function inboxEntry() { const r = S.layout.inbox; return { x: r.x + Math.floor(r.w / 2), y: r.y + r.h - 26 }; }
  function memoryPt() { const r = S.layout.memory; return { x: r.x + 12, y: r.y + 14 }; }
  function boardPt() { const r = S.layout.board; return { x: r.x + 7, y: r.y + 9 }; }
  function deskPt(id) { const d = S.deskById.get(id); return d ? { x: d.cx + 12, y: d.top - 2 } : null; }
  function resolvePoint(id, fallback) {
    if (id) {
      const d = deskPt(id);
      if (d) return d;
      const k = String(id).toLowerCase();
      if (/(inbox|queue|dispatch)/.test(k)) return inboxEntry();
      if (/rout/.test(k)) return routerPt();
      if (/(memory|store|cabinet)/.test(k)) return memoryPt();
      if (/handoff/.test(k)) return boardPt();
      if (/(done|review|result)/.test(k)) return stationSlots('done').at(0);
      if (/escalat/.test(k)) return stationSlots('esc').at(0);
    }
    return fallback;
  }

  function stationFor(t) {
    if (t.stage === 'queued') return 'inbox';
    if (t.stage === 'routing') return 'router';
    if (t.stage === 'running' || t.stage === 'delivered') return t.agent && S.deskById.has(t.agent) ? 'desk:' + t.agent : 'router';
    if (t.stage === 'escalated') return 'esc';
    return 'done'; // review + done share the shelf; review cards get an amber edge
  }

  // ── Applying a snapshot (live or demo) to the scene ──────────────────────
  function applySnapshot(st, opts) {
    const first = !S.state || (opts && opts.reset);
    const t = nowS();
    const motion = motionOn();
    S.state = st;
    S.stats.snapshots++;

    // Desks: every agent, plus an ad-hoc desk for an agent that is running a
    // task but is missing from the registry (so its card has somewhere to go).
    const agents = st.agents.slice(0, MAX_AGENTS);
    const known = new Set(agents.map((a) => a.id));
    for (const task of st.tasks) {
      if (agents.length >= MAX_AGENTS) break;
      if (task.agent && !known.has(task.agent) && (task.stage === 'running' || task.stage === 'delivered')) {
        known.add(task.agent);
        agents.push({ id: task.agent, label: task.agent, kind: task.kind || 'other', account: null,
          status: task.stage === 'running' ? 'working' : 'idle', current_task_id: task.stage === 'running' ? task.id : null,
          model: task.model, source: task.source, derived: true });
      }
    }
    // Stable first-free seat pool: an agent keeps its desk across updates.
    for (const id of Array.from(S.seats.keys())) if (!known.has(id)) S.seats.delete(id);
    const used = new Set(S.seats.values());
    const fresh = agents.filter((a) => !S.seats.has(a.id))
      .sort((a, b) => (KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind)) || a.label.localeCompare(b.label));
    for (const a of fresh) { let i = 0; while (used.has(i)) i++; S.seats.set(a.id, i); used.add(i); }
    const seatCount = agents.length ? Math.max(...agents.map((a) => S.seats.get(a.id))) + 1 : 0;

    const layoutChanged = ensureLayout(seatCount);
    S.desks = agents.map((a) => Object.assign({ agent: a, seat: S.seats.get(a.id) }, deskRect(S.seats.get(a.id))))
      .sort((a, b) => a.seat - b.seat);
    S.deskById = new Map(S.desks.map((d) => [d.agent.id, d]));

    // Cards: tasks arrive most-recent-first; stack oldest at the bottom.
    const buckets = new Map();
    for (let i = st.tasks.length - 1; i >= 0; i--) {
      const task = st.tasks[i];
      const key = stationFor(task);
      if (!buckets.has(key)) buckets.set(key, []);
      buckets.get(key).push(task);
    }
    const live = new Set();
    for (const [key, list] of buckets) {
      let slots = null;
      if (key.startsWith('desk:')) {
        const desk = S.deskById.get(key.slice(5));
        slots = { max: 4, at: (i) => deskSlot(desk, i) };
      } else {
        slots = stationSlots(key);
      }
      // Shelves show the most recent cards; the rest are counted in labels.
      const shown = list.length > slots.max ? list.slice(list.length - slots.max) : list;
      shown.forEach((task, i) => {
        const target = slots.at(i);
        live.add(task.id);
        let card = S.cards.get(task.id);
        if (!card) {
          card = { id: task.id, x: target.x, y: target.y, path: [], seg: null, station: key, target };
          S.cards.set(task.id, card);
          if (!first && motion && !layoutChanged) {
            const door = doorPt();
            card.x = door.x; card.y = door.y;
            card.path = via(null, key).concat([target]);
          }
        } else if (first || layoutChanged || !motion) {
          card.path = []; card.seg = null; card.x = target.x; card.y = target.y;
        } else if (card.station !== key) {
          card.path = via(card.station, key).concat([target]);
        } else if (card.seg || card.path.length) {
          if (card.path.length) card.path[card.path.length - 1] = target;
          else { card.seg.tx = target.x; card.seg.ty = target.y; }
        } else if (card.x !== target.x || card.y !== target.y) {
          card.path = [target];
        }
        card.station = key; card.target = target; card.task = task;
      });
    }
    for (const id of Array.from(S.cards.keys())) if (!live.has(id)) S.cards.delete(id);

    S.decision = latestDecision(st);

    // Flows: animate only the ones not seen before (never the backlog on load).
    const newFlows = [];
    for (const f of st.flows) {
      const k = [f.ts, f.type, f.task_id, f.from, f.to].join('|');
      if (!S.seenFlows.has(k)) { S.seenFlows.add(k); newFlows.push(f); }
    }
    if (S.seenFlows.size > 800) S.seenFlows = new Set(st.flows.map((f) => [f.ts, f.type, f.task_id, f.from, f.to].join('|')));
    if (!first && motion) animateFlows(newFlows.slice(0, 10).reverse(), t);

    if (S.selection) refreshSelection();
    renderStats(); renderPanel(); renderSr(); syncHotspots(); updateStatus();
    requestDraw();
  }

  function via(from, to) {
    const pts = [];
    if (to === 'inbox' || to === from) return pts;
    if (from === null && to !== 'inbox') pts.push(inboxEntry());
    if ((from === null || from === 'inbox') && to !== 'router') pts.push(routerPt());
    return pts;
  }

  function latestDecision(st) {
    let task = st.tasks.find((x) => x.stage === 'routing' && x.agent);
    if (!task) {
      const f = st.flows.find((x) => x.type === 'route');
      if (f) task = st.tasks.find((x) => x.id === f.task_id) || { id: f.task_id, agent: f.to, confidence: null, model_tier: null, model: null };
    }
    if (!task) task = st.tasks.find((x) => x.agent && (x.confidence !== null || x.model_tier));
    return task && task.agent ? { taskId: task.id, agent: task.agent, confidence: task.confidence, tier: task.model_tier, model: task.model } : null;
  }

  function animateFlows(flows, t) {
    flows.forEach((f, i) => {
      const t0 = t + i * 0.25;
      const card = f.task_id ? S.cards.get(f.task_id) : null;
      if (f.type === 'route') S.routerBlinkUntil = Math.max(S.routerBlinkUntil, t0 + 1.6);
      if (f.type === 'failover') {
        const a = deskPt(f.from), b = deskPt(f.to);
        if (card && a && card.target) { card.seg = null; card.x = a.x; card.y = a.y; card.path = [card.target]; }
        else if (a && b) addFlyer('card', a, b, t0, STAGE_EDGE.review);
        return;
      }
      if (f.type === 'memory_write') {
        addFlyer('paper', resolvePoint(f.from, routerPt()), memoryPt(), t0, null, () => { S.cabinetOpenUntil = nowS() + 1.3; });
        return;
      }
      if (f.type === 'handoff') {
        addFlyer('note', resolvePoint(f.from, routerPt()), boardPt(), t0, null, () => { S.boardFlashUntil = nowS() + 1.5; });
        return;
      }
      if (card) return; // the card's own move already shows dispatch/route/start/finish/escalate/deliver
      const map = {
        dispatch: [doorPt(), inboxEntry()], route: [inboxEntry(), routerPt()],
        start: [routerPt(), resolvePoint(f.to, null)], deliver: [routerPt(), resolvePoint(f.to, null)],
        finish: [resolvePoint(f.from, routerPt()), stationSlots('done').at(0)],
        escalate: [resolvePoint(f.from, routerPt()), stationSlots('esc').at(0)],
      }[f.type];
      if (map && map[0] && map[1]) addFlyer(f.type === 'deliver' ? 'envelope' : 'card', map[0], map[1], t0, STAGE_EDGE[f.type === 'escalate' ? 'escalated' : 'queued']);
    });
  }

  function addFlyer(kind, from, to, t0, color, onArrive) {
    if (!from || !to) return;
    if (S.flyers.length > 24) S.flyers.shift();
    const d = Math.hypot(to.x - from.x, to.y - from.y);
    S.flyers.push({ kind, fx: from.x, fy: from.y, tx: to.x, ty: to.y, t0, dur: clamp(d / 140, 0.5, 1.6), lift: Math.min(22, 6 + d * 0.2), color, onArrive, arrived: false });
  }

  function ensureLayout(seatCount) {
    const narrow = S.cssW > 0 && S.cssW < NARROW_PX;
    const key = (narrow ? 'n' : 'w') + ':' + (narrow ? Math.max(1, Math.ceil(seatCount / 3)) : Math.max(2, Math.ceil(seatCount / 4)));
    if (key === S.layoutKey && S.layout) return false;
    S.layoutKey = key;
    S.layout = computeLayout(seatCount, narrow);
    S.bgKey = '';
    sizeCanvas();
    return true;
  }

  // ── Canvas sizing (devicePixelRatio aware) ────────────────────────────────
  function sizeCanvas() {
    const L = S.layout;
    if (!L || !S.canvas) return;
    S.dpr = clamp(window.devicePixelRatio || 1, 1, 3);
    S.scale = S.cssW / L.W;
    S.cssH = Math.round(L.H * S.scale);
    S.canvas.style.height = S.cssH + 'px';
    const w = Math.max(1, Math.round(S.cssW * S.dpr)), h = Math.max(1, Math.round(S.cssH * S.dpr));
    if (S.canvas.width !== w) S.canvas.width = w;
    if (S.canvas.height !== h) S.canvas.height = h;
    if (!S.world) { S.world = document.createElement('canvas'); S.wctx = S.world.getContext('2d'); }
    S.world.width = L.W; S.world.height = L.H;
    S.fitCache.clear();
  }

  function onResize() {
    const stage = S.els && S.els.stage;
    if (!stage) return;
    const w = Math.floor(stage.clientWidth);
    if (!w || w === S.cssW) return;
    const wasNarrow = S.cssW > 0 && S.cssW < NARROW_PX;
    S.cssW = w;
    if (!S.layout) return;
    if ((w < NARROW_PX) !== wasNarrow) {
      S.layoutKey = '';
      if (S.state) { applySnapshot(S.state, { reset: true }); return; }
    }
    sizeCanvas();
    syncHotspots(true);
    requestDraw();
  }

  // ── Sprites ──────────────────────────────────────────────────────────────
  // 12x13 seated avatars (front view). Letters index a per-kind palette.
  const HUMAN = [
    '...HHHHHH...',
    '..HHHHHHHH..',
    '..HSSSSSSH..',
    '..SSESSESS..',
    '..SSSSSSSS..',
    '...SSMMSS...',
    '....SSSS....',
    '..KKKSSKKK..',
    '.KKKKKKKKKK.',
    'KKKKKKKKKKKK',
    'KkKKKKKKKKkK',
    'KkKKKKKKKKkK',
    'KkKKKKKKKKkK',
  ];
  const ROBOT = [
    '.....AA.....',
    '......m.....',
    '..MMMMMMMM..',
    '..MDDDDDDM..',
    '..MDEDDEDM..',
    '..MDDDDDDM..',
    '..MMMMMMMM..',
    '....mmmm....',
    '.KKKKKKKKKK.',
    'KKKKKKKKKKKK',
    'KkKKKLLKKKkK',
    'KkKKKKKKKKkK',
    'KkKKKKKKKKkK',
  ];

  function toGrey(hex) {
    const m = /^#([0-9a-f]{6})$/i.exec(hex || '');
    if (!m) return hex;
    const n = parseInt(m[1], 16);
    const g = Math.round(((n >> 16) * 0.3 + ((n >> 8) & 255) * 0.59 + (n & 255) * 0.11) * 0.55 + 18);
    const h = clamp(g, 0, 255).toString(16).padStart(2, '0');
    return '#' + h + h + h;
  }

  const spriteCache = new Map();
  function avatarSprite(kind, variant) {
    const key = kind + '|' + variant;
    if (spriteCache.has(key)) return spriteCache.get(key);
    const k = KINDS[kind] || KINDS.other;
    const grey = variant === 'grey';
    const closed = variant === 'blink' || grey;
    const robot = k.prop === 'robot';
    const pal = robot
      ? { A: '#22D3EE', m: '#64748B', M: '#CBD5E1', D: '#0F172A', E: closed ? '#0F172A' : '#22D3EE', K: k.shirt, k: k.shade, L: '#FDE047' }
      : { H: k.hair, S: k.skin, E: closed ? k.skin : '#1F1A17', M: '#9F5A4E', K: k.shirt, k: k.shade };
    if (closed && !robot) pal.E = '#7A5A48';
    const c = document.createElement('canvas');
    c.width = 12; c.height = 13;
    const g = c.getContext('2d');
    const put = (x, y, col) => { g.fillStyle = grey ? toGrey(col) : col; g.fillRect(x, y, 1, 1); };
    const tpl = robot ? ROBOT : HUMAN;
    tpl.forEach((row, y) => { for (let x = 0; x < row.length; x++) { const ch = row[x]; if (ch !== '.' && pal[ch]) put(x, y, pal[ch]); } });
    if (closed && !robot) { put(4, 3, '#7A5A48'); put(7, 3, '#7A5A48'); }
    // Kind accessories (original designs, baked into the sprite).
    if (k.prop === 'headset') {
      for (let x = 4; x <= 7; x++) put(x, 0, '#D4D4D8');
      [2, 3, 4].forEach((y) => { put(1, y, '#C4B5FD'); put(10, y, '#C4B5FD'); });
      put(10, 5, '#D4D4D8'); put(9, 6, '#D4D4D8');
    } else if (k.prop === 'cap') {
      for (let x = 3; x <= 8; x++) put(x, 0, '#0F766E');
      for (let x = 2; x <= 9; x++) put(x, 1, '#0F766E');
      for (let x = 1; x <= 7; x++) put(x, 2, '#134E4A');
      [3, 5, 6, 8].forEach((x) => put(x, 3, '#111827'));
    } else if (k.prop === 'orbit') {
      put(8, 9, '#FDE68A'); put(8, 10, '#FDE68A');
    } else if (k.prop === 'keycard') {
      put(4, 7, '#E5E7EB'); put(3, 8, '#E5E7EB'); put(3, 9, '#FACC15'); put(4, 9, '#FACC15'); put(3, 10, '#FACC15'); put(4, 10, '#CA8A04');
    } else if (k.prop === 'pencil') {
      put(10, 0, '#F9A8D4'); put(10, 1, '#FACC15'); put(10, 2, '#FACC15'); put(10, 3, '#44403C');
      put(5, 7, '#FEF3C7'); put(6, 7, '#FEF3C7');
    } else if (k.prop === 'gloves') {
      for (let x = 2; x <= 9; x++) put(x, 1, '#EA580C');
      put(1, 2, '#EA580C'); put(0, 3, '#EA580C');
    }
    spriteCache.set(key, c);
    return c;
  }

  function px(g, x, y, w, h, col) { g.fillStyle = col; g.fillRect(Math.round(x), Math.round(y), w, h); }

  // ── Static background: floor, wall, stations (rebuilt on layout change) ──
  function buildBg() {
    const L = S.layout;
    if (!S.bg) S.bg = document.createElement('canvas');
    S.bg.width = L.W; S.bg.height = L.H;
    const g = S.bg.getContext('2d');
    g.imageSmoothingEnabled = false;
    // Floor: carpet tiles.
    for (let y = L.wallH; y < L.H; y += 16) for (let x = 0; x < L.W; x += 16) {
      px(g, x, y, 16, 16, ((x + y) / 16) % 2 ? C.floorA : C.floorB);
    }
    for (let y = L.wallH; y < L.H; y += 16) px(g, 0, y, L.W, 1, C.floorLine);
    // Wall with baseboard.
    px(g, 0, 0, L.W, L.wallH, C.wall);
    px(g, 0, 0, L.W, 2, C.wallTop);
    px(g, 0, L.wallH - 3, L.W, 3, C.baseboard);
    // Windows fill the gaps between the door and the handoff board.
    const blocked = [L.door, L.board];
    for (let x = L.door.x + L.door.w + 8; x + 30 <= L.W - 4; x += 40) {
      if (blocked.some((b) => x + 30 > b.x - 4 && x < b.x + b.w + 4)) continue;
      px(g, x, 7, 30, 20, C.frame);
      px(g, x + 2, 9, 12, 16, C.sky); px(g, x + 16, 9, 12, 16, C.sky);
      const h = hash('w' + x);
      for (let s = 0; s < 3; s++) px(g, x + 3 + ((h >> (s * 4)) % 10) + (s % 2) * 14, 10 + ((h >> (s * 3 + 5)) % 12), 1, 1, C.star);
    }
    // Door.
    const d = L.door;
    px(g, d.x - 2, d.y - 2, d.w + 4, d.h + 2, C.woodShadow);
    px(g, d.x, d.y, d.w, d.h, '#4B3A2A');
    px(g, d.x + 2, d.y + 3, d.w - 4, 9, '#5A4633'); px(g, d.x + 2, d.y + 15, d.w - 4, 11, '#5A4633');
    px(g, d.x + d.w - 5, d.y + 15, 2, 2, '#FACC15');
    // Handoff board (cork) with a pinned note.
    const b = L.board;
    px(g, b.x, b.y, b.w, b.h, C.boardFrame);
    px(g, b.x + 2, b.y + 2, b.w - 4, b.h - 4, C.cork);
    for (let i = 0; i < 18; i++) { const h = hash('c' + i + b.x); px(g, b.x + 3 + (h % (b.w - 6)), b.y + 3 + ((h >> 8) % (b.h - 6)), 1, 1, C.corkDark); }
    px(g, b.x + 3, b.y + 5, 11, 12, C.note); px(g, b.x + 5, b.y + 9, 7, 1, '#CA8A04'); px(g, b.x + 5, b.y + 12, 5, 1, '#CA8A04');
    px(g, b.x + 8, b.y + 4, 2, 2, C.bad);
    // paper strip the hi-res overlay writes the baton on (dark text, light paper)
    px(g, b.x + 16, b.y + 4, b.w - 20, b.h - 8, C.paper); px(g, b.x + 16, b.y + b.h - 5, b.w - 20, 1, C.paperLine);
    px(g, b.x + 18, b.y + 3, 2, 2, '#2563EB'); px(g, b.x + b.w - 7, b.y + 3, 2, 2, '#2563EB');
    // Wall clock.
    const cx = L.narrow ? L.W - 12 : L.door.x + L.door.w + 132;
    if (!blocked.some((bb) => cx + 4 > bb.x && cx - 4 < bb.x + bb.w)) {
      px(g, cx - 4, 8, 9, 9, '#E5E7EB'); px(g, cx - 3, 9, 7, 7, '#F8FAFC'); px(g, cx, 10, 1, 3, C.ink); px(g, cx, 12, 3, 1, C.ink);
    }
    drawInbox(g, L.inbox); drawRouterStatic(g, L.router); drawShelf(g, L.done); drawEscCorner(g, L.esc); drawCabinet(g, L.memory, false);
    for (const p of L.plants) drawPlant(g, p.x, p.y);
    // Spare floor under the desks gets a break area (rug, water cooler, sofa).
    const spare = L.H - (L.desks.y + L.rows * L.modH);
    if (!L.narrow && spare >= 36) drawBreakArea(g, L.desks.x + Math.floor(L.desks.w / 2), L.desks.y + L.rows * L.modH + 6);
    S.bgKey = S.layoutKey;
  }

  function rug(g, r, col, edge) { px(g, r.x, r.y + 10, r.w, r.h - 10, edge); px(g, r.x + 1, r.y + 11, r.w - 2, r.h - 12, col); }

  function drawInbox(g, r) {
    rug(g, r, C.rug, C.rugEdge);
    const ty = r.y + r.h - 14;
    px(g, r.x + 5, ty, r.w - 10, 2, C.metalLight);
    px(g, r.x + 5, ty + 2, r.w - 10, 6, C.metal);
    px(g, r.x + 5, ty + 8, r.w - 10, 1, C.metalDark);
    px(g, r.x + 7, ty + 9, 2, 3, C.metalDark); px(g, r.x + r.w - 9, ty + 9, 2, 3, C.metalDark);
  }

  function drawRouterStatic(g, r) {
    rug(g, r, '#262338', '#2F2B45');
    const mx = r.x + Math.floor(r.w / 2) - 15, my = r.y + 16;
    // funnel + sorting machine body
    px(g, mx + 9, my - 3, 12, 3, C.metalDark); px(g, mx + 11, my - 1, 8, 2, C.metal);
    px(g, mx, my + 1, 30, 30, C.metalDark); px(g, mx + 1, my + 2, 28, 28, C.metal); px(g, mx + 1, my + 2, 28, 1, C.metalLight);
    px(g, mx + 4, my + 5, 22, 10, '#0B1220');
    px(g, mx + 3, my + 25, 24, 3, C.metalDark);
    // counter
    const cy = r.y + 48;
    px(g, r.x + 6, cy, r.w - 12, 2, C.woodLight); px(g, r.x + 6, cy + 2, r.w - 12, 3, C.wood);
    px(g, r.x + 7, cy + 5, r.w - 14, 7, C.woodDark); px(g, r.x + 7, cy + 5, r.w - 14, 1, C.woodShadow);
  }

  function drawShelf(g, r) {
    rug(g, r, C.rug, C.rugEdge);
    px(g, r.x + 4, r.y + 12, r.w - 8, r.h - 12, C.woodShadow);
    px(g, r.x + 6, r.y + 14, r.w - 12, r.h - 16, '#2A2018');
    for (const y of shelfPlanks(r)) { px(g, r.x + 5, y, r.w - 10, 2, C.woodLight); px(g, r.x + 5, y + 2, r.w - 10, 1, C.woodDark); }
  }

  function drawEscCorner(g, r) {
    px(g, r.x, r.y + 10, r.w, r.h - 10, C.escDark);
    for (let x = r.x; x < r.x + r.w; x += 4) { px(g, x, r.y + 10, 2, 2, C.hazard); px(g, x + 2, r.y + r.h - 2, 2, 2, C.hazard); }
    px(g, r.x + 2, r.y + 13, r.w - 4, r.h - 16, C.esc);
    // traffic cone
    const cx = r.x + 8, cy = r.y + r.h - 8;
    px(g, cx - 4, cy, 9, 2, '#9A3412'); px(g, cx - 3, cy - 3, 7, 3, '#F97316'); px(g, cx - 2, cy - 6, 5, 3, '#FFFFFF');
    px(g, cx - 1, cy - 9, 3, 3, '#F97316'); px(g, cx, cy - 11, 1, 2, '#F97316');
    // desk bell
    const bx = r.x + r.w - 14, by = r.y + 18;
    px(g, bx, by + 4, 8, 1, '#78716C'); px(g, bx + 1, by + 1, 6, 3, '#FACC15'); px(g, bx + 3, by, 2, 1, '#FACC15'); px(g, bx + 2, by + 1, 1, 1, '#FEF08A');
  }

  function drawCabinet(g, r, open) {
    const x = r.x + 6, y = r.y + 12, w = 26, h = 40;
    px(g, x - 1, y - 1, w + 2, h + 2, C.metalDark);
    px(g, x, y, w, h, C.metal);
    for (let i = 0; i < 3; i++) {
      const dy = y + 2 + i * 13;
      const off = open && i === 0 ? 3 : 0;
      if (off) px(g, x + 2, dy, w - 4, 11, '#1F2937');
      px(g, x + 2, dy + off, w - 4, 11, C.metalLight);
      px(g, x + 3, dy + off + 1, w - 6, 9, C.metal);
      px(g, x + 10, dy + off + 4, 6, 2, C.metalDark);
      if (off) { px(g, x + 5, dy - 3, 8, 5, C.paper); px(g, x + 14, dy - 2, 7, 4, '#E0F2FE'); }
    }
  }

  function drawBreakArea(g, cx, y) {
    px(g, cx - 60, y + 4, 120, 26, '#202633'); px(g, cx - 59, y + 5, 118, 24, '#242B39');
    // water cooler
    px(g, cx - 44, y + 2, 10, 8, '#7DD3FC'); px(g, cx - 43, y + 3, 8, 3, '#BAE6FD');
    px(g, cx - 45, y + 10, 12, 16, '#E5E7EB'); px(g, cx - 45, y + 10, 12, 1, '#F8FAFC'); px(g, cx - 41, y + 14, 3, 2, '#3B82F6');
    // sofa
    px(g, cx - 18, y + 8, 44, 8, '#4C3B6B'); px(g, cx - 18, y + 8, 44, 1, '#5E4A85');
    px(g, cx - 20, y + 14, 48, 9, '#5B4880'); px(g, cx - 20, y + 14, 4, 9, '#4C3B6B'); px(g, cx + 24, y + 14, 4, 9, '#4C3B6B');
    px(g, cx - 18, y + 23, 3, 3, C.woodShadow); px(g, cx + 23, y + 23, 3, 3, C.woodShadow);
    drawPlant(g, cx + 44, y + 6);
  }

  function drawPlant(g, x, y) {
    px(g, x - 3, y + 8, 7, 6, '#9A3412'); px(g, x - 3, y + 8, 7, 1, '#C2410C');
    px(g, x - 1, y, 2, 8, '#15803D'); px(g, x - 5, y + 2, 4, 3, '#16A34A'); px(g, x + 1, y - 1, 4, 3, '#22C55E'); px(g, x - 3, y - 3, 3, 3, '#4ADE80');
  }

  // ── Per-frame drawing ─────────────────────────────────────────────────────
  function requestDraw() {
    if (!S.active || S.raf) return;
    S.raf = requestAnimationFrame(frame);
  }

  function frame(ts) {
    S.raf = 0;
    if (!S.active) return;
    const motion = motionOn();
    if (!motion || ts - S.lastDraw >= FRAME_MS - 3) { S.lastDraw = ts; draw(); }
    if (motion) S.raf = requestAnimationFrame(frame);
  }

  function draw() {
    const L = S.layout;
    if (!L || !S.ctx || !S.state || !S.cssW) return;
    if (S.bgKey !== S.layoutKey || !S.bg) buildBg();
    const t = nowS();
    const motion = motionOn();
    const g = S.wctx;
    g.imageSmoothingEnabled = false;
    g.drawImage(S.bg, 0, 0);
    S.hits = [];
    hit(L.inbox, 'station:inbox'); hit(L.router, 'station:router'); hit(L.done, 'station:done'); hit(L.esc, 'station:esc');
    hit(L.memory, 'memory'); hit(L.board, 'handoff');

    drawRouterLive(g, L.router, t, motion);
    if (t < S.cabinetOpenUntil) drawCabinet(g, L.memory, true);
    if (t < S.boardFlashUntil && Math.floor(t * 6) % 2 === 0) { const b = L.board; px(g, b.x + 1, b.y + 1, b.w - 2, 1, C.select); px(g, b.x + 1, b.y + b.h - 2, b.w - 2, 1, C.select); }

    for (const desk of S.desks) drawDesk(g, desk, t, motion);

    // Cards: resting ones first, travelling ones on top.
    const moving = [];
    for (const card of S.cards.values()) {
      if (motion) advanceCard(card, t);
      if (card.seg || card.path.length) moving.push(card); else drawCard(g, card, t, motion);
    }
    for (const card of moving) drawCard(g, card, t, motion);
    drawFlyers(g, t, motion);
    drawSelection(g);

    const c = S.ctx;
    c.setTransform(1, 0, 0, 1, 0, 0);
    c.imageSmoothingEnabled = false;
    c.clearRect(0, 0, S.canvas.width, S.canvas.height);
    c.drawImage(S.world, 0, 0, S.canvas.width, S.canvas.height);
    c.setTransform(S.dpr, 0, 0, S.dpr, 0, 0);
    drawOverlay(c, t, motion);
    S.stats.frames++;
  }

  function hit(r, key) { S.hits.push({ x: r.x, y: r.y, w: r.w, h: r.h, key }); }

  function drawRouterLive(g, r, t, motion) {
    const mx = r.x + Math.floor(r.w / 2) - 15, my = r.y + 16;
    const blinking = t < S.routerBlinkUntil;
    const routing = S.state.tasks.some((x) => x.stage === 'routing');
    const on = blinking || routing;
    // screen: an arrow that sweeps while a decision is being made
    const phase = motion ? Math.floor(t * 8) % 14 : 7;
    if (on) {
      px(g, mx + 6 + phase, my + 9, 4, 2, '#A78BFA'); px(g, mx + 9 + phase, my + 8, 1, 4, '#A78BFA');
    } else {
      px(g, mx + 7, my + 9, 2, 2, '#334155'); px(g, mx + 12, my + 9, 2, 2, '#334155'); px(g, mx + 17, my + 9, 2, 2, '#334155');
    }
    const lamps = ['#22C55E', '#F59E0B', '#06B6D4'];
    lamps.forEach((col, i) => {
      const lit = on ? (!motion || (Math.floor(t * 5) + i) % 3 !== 0) : i === 0;
      px(g, mx + 6 + i * 7, my + 19, 4, 3, lit ? col : '#1F2937');
    });
  }

  function drawDesk(g, d, t, motion) {
    const a = d.agent;
    const k = KINDS[a.kind] || KINDS.other;
    const grey = a.status === 'offline';
    const working = a.status === 'working';
    const cx = d.cx, top = d.top;
    const phase = (hash(a.id) % 100) / 17;
    const tone = (col) => (grey ? toGrey(col) : col);
    hit(agentRect(d), 'agent:' + a.id);
    // chair back
    px(g, cx - 8, top - 15, 16, 16, tone(C.chair)); px(g, cx - 7, top - 16, 14, 1, tone(C.chair)); px(g, cx - 8, top - 15, 16, 1, tone(C.chairLight));
    // antigravity: a tiny moon orbiting the head (behind on the far half)
    const orbit = k.prop === 'orbit' && !grey;
    const ang = motion ? t * 2.4 + phase : 0.6;
    const ox = cx + Math.round(Math.cos(ang) * 8) - 1, oy = top - 12 + Math.round(Math.sin(ang) * 2);
    if (orbit && Math.sin(ang) < 0) px(g, ox, oy, 2, 2, '#FDE68A');
    // avatar
    let variant = grey ? 'grey' : 'n';
    if (!grey && motion && ((t + phase) % 4.2) < 0.14) variant = 'blink';
    const bob = working && motion && Math.floor((t + phase) * 2) % 2 ? 1 : 0;
    g.drawImage(avatarSprite(a.kind, variant), cx - 6, top - 11 + bob);
    if (orbit && Math.sin(ang) >= 0) px(g, ox, oy, 2, 2, '#FDE68A');
    // desk
    const x = cx - 24;
    px(g, x, top, 48, 1, tone(C.woodLight)); px(g, x, top + 1, 48, 4, tone(C.wood));
    px(g, x + 1, top + 5, 46, 9, tone(C.woodDark)); px(g, x + 1, top + 5, 46, 1, tone(C.woodShadow));
    px(g, cx + 12, top + 9, 6, 1, tone(C.woodLight));
    px(g, x + 2, top + 14, 2, 4, tone(C.woodShadow)); px(g, x + 44, top + 14, 2, 4, tone(C.woodShadow));
    // mug (+ steam while idle)
    px(g, cx - 20, top - 3, 4, 4, tone('#F2EDE2')); px(g, cx - 20, top - 1, 4, 1, tone(k.color)); px(g, cx - 16, top - 2, 1, 2, tone('#D9D2C4'));
    if (!grey && !working && motion) { const s = Math.floor((t + phase) * 3) % 4; px(g, cx - 19 + (s % 2), top - 5 - s, 1, 1, 'rgba(226,232,240,.55)'); }
    // hands on the desk (typing alternates them), then the laptop lid in front
    if (!grey) {
      const hand = k.prop === 'gloves' ? '#F8FAFC' : (k.prop === 'robot' ? '#CBD5E1' : k.skin);
      const tick = working && motion ? Math.floor((t + phase) * 8) % 2 : 0;
      px(g, cx - 9, top - 3, 3, 2, k.shirt); px(g, cx + 6, top - 3, 3, 2, k.shirt);
      px(g, cx - 10, top - 1 - tick, 2, 2, hand); px(g, cx + 8, top - 2 + tick, 2, 2, hand);
    }
    if (grey) {
      px(g, cx - 7, top - 1, 14, 1, tone(C.lid));
    } else {
      px(g, cx - 7, top - 5, 14, 5, C.lid); px(g, cx - 7, top - 5, 14, 1, C.lidLight); px(g, cx - 8, top, 16, 1, C.keys);
      const blocked = a.status === 'blocked';
      const glow = working ? (motion && Math.floor((t + phase) * 3) % 3 === 0 ? '#E0F2FE' : k.color) : (blocked ? C.bad : '#52525B');
      px(g, cx - 2, top - 4, 4, 3, glow);
      if (working) px(g, cx - 6, top - 6, 12, 1, 'rgba(191,227,255,.35)');
    }
    // blocked: red "!" bubble
    if (a.status === 'blocked' && (!motion || (t % 1.2) < 0.85)) {
      const bx = cx + 6, by = top - 25;
      px(g, bx, by, 8, 9, '#7F1D1D'); px(g, bx + 1, by + 1, 6, 7, C.bad); px(g, bx + 1, by + 9, 2, 2, '#7F1D1D');
      px(g, bx + 3, by + 2, 2, 3, '#FFFFFF'); px(g, bx + 3, by + 6, 2, 1, '#FFFFFF');
    }
  }

  function drawCard(g, card, t, motion) {
    const task = card.task;
    if (!task) return;
    const x = Math.round(card.x), y = Math.round(card.y);
    if (task.stage === 'delivered') {
      const bob = motion && !card.seg ? Math.round(Math.sin(t * 3 + (hash(card.id) % 7)) * 0.8) : 0;
      drawEnvelope(g, x, y - 1 + bob);
      S.hits.push({ x: x - 1, y: y - 2, w: 10, h: 9, key: 'task:' + task.id });
      return;
    }
    const edge = task.stage === 'running' ? (KINDS[task.kind] || KINDS[(S.deskById.get(task.agent) || { agent: {} }).agent.kind] || KINDS.other).color : (STAGE_EDGE[task.stage] || '#A1A1AA');
    px(g, x + 1, y + 5, 7, 1, 'rgba(0,0,0,.35)');
    px(g, x, y, 7, 5, C.paper); px(g, x, y, 7, 1, edge);
    px(g, x + 1, y + 2, 5, 1, C.paperLine); px(g, x + 1, y + 3, 3, 1, C.paperLine);
    if (task.stage === 'done') { px(g, x + 5, y + 3, 1, 1, C.ok); px(g, x + 6, y + 2, 1, 1, C.ok); }
    if (task.stage === 'escalated') px(g, x + 5, y + 2, 1, 2, C.bad);
    S.hits.push({ x: x - 1, y: y - 1, w: 9, h: 7, key: 'task:' + task.id });
  }

  function drawEnvelope(g, x, y) {
    px(g, x, y, 8, 6, '#E7E0CF'); px(g, x, y, 8, 1, '#B8AE98');
    px(g, x + 1, y + 1, 1, 1, '#B8AE98'); px(g, x + 2, y + 2, 1, 1, '#B8AE98'); px(g, x + 5, y + 2, 1, 1, '#B8AE98'); px(g, x + 6, y + 1, 1, 1, '#B8AE98');
    px(g, x + 3, y + 3, 2, 1, C.bad);
  }

  function advanceCard(card, t) {
    for (let guard = 0; guard < 8; guard++) {
      if (!card.seg) {
        if (!card.path.length) return;
        const p = card.path.shift();
        const dist = Math.hypot(p.x - card.x, p.y - card.y);
        if (dist < 0.5) { card.x = p.x; card.y = p.y; continue; }
        card.seg = { fx: card.x, fy: card.y, tx: p.x, ty: p.y, t0: t, dur: clamp(dist / 150, 0.35, 1.1), lift: Math.min(16, dist * 0.2) };
      }
      const s = card.seg;
      const p = clamp((t - s.t0) / s.dur, 0, 1);
      const e = ease(p);
      card.x = s.fx + (s.tx - s.fx) * e;
      card.y = s.fy + (s.ty - s.fy) * e - Math.sin(Math.PI * p) * s.lift;
      if (p < 1) return;
      card.x = s.tx; card.y = s.ty; card.seg = null;
    }
  }

  function drawFlyers(g, t, motion) {
    if (!motion) { S.flyers.length = 0; return; }
    S.flyers = S.flyers.filter((f) => t < f.t0 + f.dur + 0.35);
    for (const f of S.flyers) {
      if (t < f.t0) continue;
      const p = clamp((t - f.t0) / f.dur, 0, 1);
      if (p >= 1) {
        if (!f.arrived) { f.arrived = true; if (f.onArrive) f.onArrive(); }
        const b = (t - f.t0 - f.dur) / 0.35;
        const r = Math.round(2 + b * 5);
        const col = 'rgba(253,224,71,' + (1 - b).toFixed(2) + ')';
        px(g, f.tx + 2, f.ty - r, 1, 1, col); px(g, f.tx + 2, f.ty + 3 + r, 1, 1, col);
        px(g, f.tx - r, f.ty + 2, 1, 1, col); px(g, f.tx + 4 + r, f.ty + 2, 1, 1, col);
        continue;
      }
      const e = ease(p);
      const x = Math.round(f.fx + (f.tx - f.fx) * e), y = Math.round(f.fy + (f.ty - f.fy) * e - Math.sin(Math.PI * p) * f.lift);
      if (f.kind === 'envelope') drawEnvelope(g, x, y);
      else if (f.kind === 'note') { px(g, x, y, 5, 5, C.note); px(g, x + 1, y + 2, 3, 1, '#CA8A04'); }
      else if (f.kind === 'paper') { px(g, x, y, 5, 6, C.paper); px(g, x + 1, y + 2, 3, 1, C.paperLine); px(g, x + 1, y + 4, 3, 1, C.paperLine); }
      else { px(g, x, y, 7, 5, C.paper); px(g, x, y, 7, 1, f.color || STAGE_EDGE.queued); px(g, x + 1, y + 2, 5, 1, C.paperLine); }
    }
  }

  function selectionRect() {
    const sel = S.selection;
    if (!sel) return null;
    if (sel.type === 'agent') { const d = S.deskById.get(sel.id); return d ? agentRect(d) : null; }
    if (sel.type === 'task') { const c = S.cards.get(sel.id); return c ? { x: Math.round(c.x) - 2, y: Math.round(c.y) - 2, w: 11, h: 9 } : null; }
    const L = S.layout;
    const r = { 'station:inbox': L.inbox, 'station:router': L.router, 'station:done': L.done, 'station:esc': L.esc, memory: L.memory, handoff: L.board }[sel.key];
    return r || null;
  }

  function drawSelection(g) {
    const r = selectionRect();
    if (!r) return;
    px(g, r.x, r.y, r.w, 1, C.select); px(g, r.x, r.y + r.h - 1, r.w, 1, C.select);
    px(g, r.x, r.y, 1, r.h, C.select); px(g, r.x + r.w - 1, r.y, 1, r.h, C.select);
  }

  // ── Hi-res overlay: labels, the router decision, handoff text, bubbles ───
  function fit(c, text, maxW) {
    const s = String(text || '');
    if (maxW <= 8) return '';
    const key = c.font + '|' + Math.round(maxW) + '|' + s;
    const hitc = S.fitCache.get(key);
    if (hitc !== undefined) return hitc;
    let out = s;
    if (c.measureText(s).width > maxW) {
      let lo = 0, hi = s.length;
      while (lo < hi) { const mid = (lo + hi + 1) >> 1; if (c.measureText(s.slice(0, mid) + '…').width <= maxW) lo = mid; else hi = mid - 1; }
      out = lo ? s.slice(0, lo) + '…' : '';
    }
    if (S.fitCache.size > 600) S.fitCache.clear();
    S.fitCache.set(key, out);
    return out;
  }

  // Greedy word wrap into at most two lines; the second is ellipsised.
  function wrap2(c, text, maxW) {
    const key = 'w2|' + c.font + '|' + Math.round(maxW) + '|' + text;
    const cached = S.fitCache.get(key);
    if (cached !== undefined) return cached;
    const words = String(text || '').split(/\s+/).filter(Boolean);
    let line1 = '';
    let i = 0;
    for (; i < words.length; i++) {
      const next = line1 ? line1 + ' ' + words[i] : words[i];
      if (c.measureText(next).width > maxW) break;
      line1 = next;
    }
    let out;
    if (!line1) out = [fit(c, text, maxW), ''];               // one very long token
    else out = [line1, i < words.length ? fit(c, words.slice(i).join(' '), maxW) : ''];
    S.fitCache.set(key, out);
    return out;
  }

  function roundRect(c, x, y, w, h, r) {
    c.beginPath();
    c.moveTo(x + r, y); c.lineTo(x + w - r, y); c.quadraticCurveTo(x + w, y, x + w, y + r);
    c.lineTo(x + w, y + h - r); c.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
    c.lineTo(x + r, y + h); c.quadraticCurveTo(x, y + h, x, y + h - r);
    c.lineTo(x, y + r); c.quadraticCurveTo(x, y, x + r, y); c.closePath();
  }

  function agentLabel(id) { const d = id ? S.deskById.get(id) : null; return d ? d.agent.label : (id || '—'); }

  function stationLabel(c, r, title, count, fs, s, color) {
    c.font = `700 ${fs}px ${SANS}`;
    const x = (r.x + 2) * s, y = (r.y + 1) * s + fs;
    c.fillStyle = 'rgba(10,10,10,.55)';
    const tw = c.measureText(title).width;
    c.font = `600 ${fs}px ${MONO}`;
    const cw = count === null ? 0 : c.measureText(String(count)).width + 6;
    roundRect(c, x - 3, y - fs - 1, tw + cw + 7, fs + 5, 4); c.fill();
    c.font = `700 ${fs}px ${SANS}`; c.fillStyle = '#E4E4E7'; c.fillText(title, x, y);
    if (count !== null) { c.font = `600 ${fs}px ${MONO}`; c.fillStyle = color || '#A5B4FC'; c.fillText(String(count), x + tw + 5, y); }
  }

  function drawOverlay(c, t, motion) {
    const L = S.layout, s = S.scale, st = S.state;
    const fs = clamp(Math.round(s * 6.2), 9, 12);
    c.textBaseline = 'alphabetic';
    c.textAlign = 'left';
    const count = (stage) => st.tasks.filter((x) => x.stage === stage).length;
    stationLabel(c, L.inbox, 'Inbox', count('queued'), fs, s, '#67E8F9');
    stationLabel(c, L.router, 'Router', count('routing') || null, fs, s, '#C4B5FD');
    stationLabel(c, L.done, 'Done', count('done') + count('review'), fs, s, '#86EFAC');
    stationLabel(c, L.esc, 'Escalations', count('escalated'), fs, s, '#FCA5A5');
    stationLabel(c, L.memory, 'Memory', st.memory.entries, fs, s, '#FDE68A');

    // Router decision: agent, confidence, tier chip.
    const dec = S.decision;
    const r = L.router;
    const dy = (r.y + r.h - 5) * s;
    c.font = `600 ${fs - 1}px ${SANS}`;
    if (dec) {
      const pct = dec.confidence === null || dec.confidence === undefined ? '' : ` ${Math.round(dec.confidence * 100)}%`;
      let x = (r.x + 3) * s;
      const tier = dec.tier ? String(dec.tier) : '';
      c.font = `700 ${fs - 2}px ${SANS}`;
      const tierW = tier ? c.measureText(tier).width + 8 : 0;
      c.font = `600 ${fs - 1}px ${SANS}`;
      const txt = fit(c, '→ ' + agentLabel(dec.agent) + pct, r.w * s - 8 - tierW);
      c.fillStyle = '#E4E4E7'; c.fillText(txt, x, dy);
      if (tier) {
        x += c.measureText(txt).width + 4;
        const col = TIER_COLORS[tier.toLowerCase()] || '#A1A1AA';
        c.font = `700 ${fs - 2}px ${SANS}`;
        roundRect(c, x, dy - fs + 2, tierW, fs, fs / 2); c.fillStyle = 'rgba(10,10,10,.6)'; c.fill();
        c.strokeStyle = col; c.lineWidth = 1; c.stroke();
        c.fillStyle = col; c.fillText(tier, x + 4, dy - 1);
      }
    } else {
      c.fillStyle = '#71717A'; c.fillText(fit(c, 'Waiting for a task', r.w * s - 8), (r.x + 3) * s, dy);
    }

    // Handoff board: the current baton's next step.
    const b = L.board, h = st.handoff;
    const bx = (b.x + 19) * s, bw = (b.w - 24) * s;
    c.font = `700 ${fs - 1}px ${SANS}`;
    c.fillStyle = '#1C1917';
    const head = ['Handoff', h.status, h.agent].filter(Boolean).join(' · ');
    c.fillText(fit(c, head, bw), bx, (b.y + 4) * s + fs - 1);
    c.font = `600 ${fs - 1}px ${SANS}`;
    c.fillStyle = '#292524';
    c.fillText(fit(c, h.next ? 'Next: ' + h.next : (h.title || 'No baton in flight'), bw), bx, (b.y + 4) * s + fs * 2 + 1);

    // Desk labels.
    for (const d of S.desks) {
      const a = d.agent;
      const lx = d.cx * s, ly = (d.top + 22) * s;
      const mw = d.w * s - 6;
      c.textAlign = 'center';
      c.font = `700 ${fs}px ${SANS}`;
      const name = fit(c, a.label, mw - 12);
      const nw = c.measureText(name).width;
      c.fillStyle = STATUS_COLORS[a.status] || '#A1A1AA';
      c.beginPath(); c.arc(lx - nw / 2 - 6, ly - fs * 0.35, 3, 0, Math.PI * 2); c.fill();
      c.fillStyle = a.status === 'offline' ? '#71717A' : '#F4F4F5';
      c.fillText(name, lx + 2, ly);
      c.font = `500 ${fs - 2}px ${MONO}`;
      c.fillStyle = '#A1A1AA';
      const sub = a.status === 'offline' ? 'offline' : (a.status === 'blocked' ? 'blocked — needs help' : (a.model || a.account || KINDS[a.kind].name));
      c.fillText(fit(c, sub, mw), lx, ly + fs + 1);
      c.textAlign = 'left';
    }

    // Thought bubbles above working avatars (drawn last, on top).
    for (const d of S.desks) {
      const a = d.agent;
      if (a.status !== 'working') continue;
      const task = (a.current_task_id && st.tasks.find((x) => x.id === a.current_task_id)) || st.tasks.find((x) => x.agent === a.id && x.stage === 'running');
      const title = task ? task.title : 'Working…';
      const model = (task && task.model) || a.model;
      const maxW = Math.min(d.w * s - 6, 220);
      c.font = `600 ${fs - 1}px ${SANS}`;
      const lines = wrap2(c, title, maxW - 12).filter(Boolean);
      const w1 = Math.max(...lines.map((l) => c.measureText(l).width), 0);
      c.font = `500 ${fs - 2}px ${MONO}`;
      const l2 = model ? fit(c, model, maxW - 12) : '';
      const w2 = l2 ? c.measureText(l2).width : 0;
      const bw2 = Math.min(maxW, Math.max(w1, w2) + 12);
      const lh = fs + 1;
      const bh = lh * (lines.length + (l2 ? 1 : 0)) + 6;
      const headY = (d.top - 12) * s;
      const cx = d.cx * s;
      const x = clamp(cx - bw2 / 2, 2, S.cssW - bw2 - 2);
      const y = headY - bh - 7;
      c.fillStyle = '#FFF8E7'; c.strokeStyle = '#1C1917'; c.lineWidth = 1;
      roundRect(c, Math.round(x) + 0.5, Math.round(y) + 0.5, Math.round(bw2), Math.round(bh), 6); c.fill(); c.stroke();
      c.beginPath(); c.arc(cx + 3, headY - 5, 2.4, 0, Math.PI * 2); c.fill(); c.stroke();
      c.beginPath(); c.arc(cx + 6, headY - 1.5, 1.4, 0, Math.PI * 2); c.fill(); c.stroke();
      c.fillStyle = '#292524'; c.font = `600 ${fs - 1}px ${SANS}`;
      lines.forEach((l, i) => c.fillText(l, x + 6, y + lh * (i + 1)));
      if (l2) { c.fillStyle = '#6D28D9'; c.font = `500 ${fs - 2}px ${MONO}`; c.fillText(l2, x + 6, y + lh * (lines.length + 1) - 1); }
    }
  }

  // ── HTML: stats, status pill, banner, panel, hotspots, screen reader ─────
  function counts() {
    const st = S.state;
    const by = (list, key, v) => list.filter((x) => x[key] === v).length;
    const agents = S.desks.map((d) => d.agent);
    return {
      agents: agents.length,
      working: by(agents, 'status', 'working'), idle: by(agents, 'status', 'idle'),
      blocked: by(agents, 'status', 'blocked'), offline: by(agents, 'status', 'offline'),
      queued: by(st.tasks, 'stage', 'queued'), routing: by(st.tasks, 'stage', 'routing'), running: by(st.tasks, 'stage', 'running'),
      done: by(st.tasks, 'stage', 'done') + by(st.tasks, 'stage', 'review'), escalated: by(st.tasks, 'stage', 'escalated'),
      delivered: by(st.tasks, 'stage', 'delivered'), memory: st.memory.entries,
    };
  }

  function summaryText() {
    const n = counts();
    return `Office floor${S.mode === 'demo' ? ' (demo data)' : ''}: ${n.agents} agent${n.agents === 1 ? '' : 's'} — ${n.working} working, ${n.idle} idle, ${n.blocked} blocked, ${n.offline} offline. `
      + `Tasks: ${n.queued} queued, ${n.routing} routing, ${n.running} running, ${n.delivered} delivered to the IDE, ${n.done} done, ${n.escalated} escalated. ${n.memory} memory entries.`;
  }

  function renderStats() {
    const el = S.els.stats;
    const n = counts();
    const chip = (tone, label, value) => `<span class="office-stat"><span class="ui-dot" style="color:${tone}" aria-hidden="true"></span>${esc(label)} <b>${esc(value)}</b></span>`;
    let html = chip(STATUS_COLORS.working, 'Working', n.working) + chip(STATUS_COLORS.idle, 'Idle', n.idle)
      + chip(STATUS_COLORS.blocked, 'Blocked', n.blocked) + chip(STATUS_COLORS.offline, 'Offline', n.offline)
      + chip(STAGE_EDGE.queued, 'Queued', n.queued) + chip(STAGE_EDGE.done, 'Done', n.done)
      + chip(STAGE_EDGE.escalated, 'Escalated', n.escalated) + chip('#FDE68A', 'Memory', n.memory);
    if (S.mode === 'live' && !S.state.sources.brain_swarm) {
      html += '<span class="office-stat office-stat-note" title="The brain swarm store (BRAIN_DIR) was not found, so only Mission Control tasks are shown.">Brain swarm store not found — Mission Control only</span>';
    }
    if (html !== S.statsSig) { S.statsSig = html; el.innerHTML = html; }
    S.canvas.setAttribute('aria-label', summaryText());
  }

  function updateStatus() {
    const pill = S.els.status, text = S.els.statusText;
    if (!pill) return;
    let cls = 'ops-pill-idle', label = 'Connecting…', title = '';
    if (S.mode === 'demo') {
      cls = 'ops-pill-warn'; label = 'Demo data';
      title = 'No live agents are connected; the floor is running a simulation.';
    } else if (S.mode === 'live') {
      const ago = S.lastOkAt ? Math.round((Date.now() - S.lastOkAt) / 1000) : 0;
      if (S.fetchNote) { cls = 'ops-pill-warn'; label = S.fetchNote; title = 'Showing the last state received.'; }
      else { cls = 'ops-pill-live'; label = ago < 5 ? 'Live' : `Live · ${ago}s ago`; title = 'Polled every 3 seconds and on live events.'; }
    }
    pill.className = 'ops-pill ' + cls;
    pill.title = title;
    if (text.textContent !== label) text.textContent = label;
  }

  function showBanner(reason) {
    const el = S.els.banner;
    if (!reason) { el.hidden = true; return; }
    const hints = {
      endpoint: 'This Mission Control has no /api/office/state endpoint yet, so the office shows a simulation of how work flows between agents.',
      empty: 'Mission Control and the brain swarm report no agents or tasks yet. Dispatch a task from Overview and it will walk onto this floor.',
      auth: 'Your session could not be verified, so live office data is unavailable. Reload the page to sign in again.',
      error: 'Live office data could not be loaded' + (S.fetchNote ? ` (${S.fetchNote})` : '') + '. The dashboard keeps retrying.',
    };
    el.innerHTML = `<i class="fa-solid fa-flask" aria-hidden="true"></i><span><strong>Demo data — no live agents connected.</strong>
      <span class="office-banner-hint">${esc(hints[reason] || hints.empty)}</span></span>`;
    el.hidden = false;
  }

  function stageBadge(stage) { return ui.badge(STAGE_BADGE[stage] || 'unknown', stage || 'unknown'); }
  function tierChip(tier) {
    if (!tier) return '';
    const col = TIER_COLORS[String(tier).toLowerCase()] || '#A1A1AA';
    return `<span class="office-tier" style="color:${col}">${esc(tier)}</span>`;
  }
  function confHtml(v) {
    if (v === null || v === undefined) return '<span class="ui-muted">—</span>';
    const pct = Math.round(clamp(v, 0, 1) * 100);
    return `<span class="office-conf" aria-hidden="true"><span style="width:${pct}%"></span></span>${pct}%`;
  }
  function fmtDur(sec) {
    if (sec === null || sec === undefined) return '—';
    if (sec < 60) return `${Math.round(sec)}s`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ${Math.round(sec % 60)}s`;
    return `${Math.floor(sec / 3600)}h ${Math.round((sec % 3600) / 60)}m`;
  }
  function taskLink(t) {
    return `<button type="button" class="office-link" data-office-select="task:${esc(t.id)}">
      <span class="office-link-text">${esc(t.title)}</span>${stageBadge(t.stage)}</button>`;
  }
  function agentLink(id) {
    if (!id) return '<span class="ui-muted">—</span>';
    if (!S.deskById.has(id)) return `<span class="ui-mono">${esc(id)}</span>`;
    return `<button type="button" class="ops-id" data-office-select="agent:${esc(id)}" title="Show this agent">${esc(agentLabel(id))}</button>`;
  }
  function flowLine(f) {
    const route = [f.from, f.to].filter(Boolean).map(esc).join(' → ');
    return `<li><span class="office-flow-type">${esc(f.type)}</span> ${route ? `<span class="ui-muted">${route}</span>` : ''}
      ${f.detail ? `<div>${esc(f.detail)}</div>` : ''}<div class="ui-muted">${ui.timeTag(f.ts)}</div></li>`;
  }
  function head(eyebrow, title) {
    return `<div class="office-panel-head"><div><div class="office-eyebrow">${esc(eyebrow)}</div>
      <h3 class="office-panel-title">${esc(title)}</h3></div>
      <button type="button" class="ui-btn ui-btn-ghost office-close" data-office-close aria-label="Close details"><i class="fa-solid fa-xmark" aria-hidden="true"></i></button></div>`;
  }

  function panelTask(t) {
    const flows = S.state.flows.filter((f) => f.task_id === t.id).slice(0, 8);
    return head('Task', t.title)
      + `<div class="office-badges">${stageBadge(t.stage)}${t.requires_approval ? '<span class="ops-chip">Needs approval</span>' : ''}${S.mode === 'demo' ? '<span class="ops-chip">Demo</span>' : ''}</div>
      <dl class="office-kv">
        <dt>ID</dt><dd class="ui-mono">${esc(t.id)}</dd>
        <dt>Agent</dt><dd>${agentLink(t.agent)}${t.kind ? ` <span class="ui-muted">(${esc((KINDS[t.kind] || KINDS.other).name)})</span>` : ''}</dd>
        <dt>Model</dt><dd><span class="ui-mono">${esc(t.model || '—')}</span> ${tierChip(t.model_tier)}</dd>
        <dt>Why this agent</dt><dd>${esc(t.routing_reason || '—')}</dd>
        <dt>Confidence</dt><dd>${confHtml(t.confidence)}</dd>
        <dt>Complexity</dt><dd>${esc(t.complexity || '—')}${t.risk ? ` · risk ${esc(t.risk)}` : ''}</dd>
        <dt>Created</dt><dd>${ui.timeTag(t.created_at)}</dd>
        <dt>Started</dt><dd>${ui.timeTag(t.started_at)}</dd>
        <dt>Finished</dt><dd>${ui.timeTag(t.completed_at)}</dd>
        <dt>Duration</dt><dd>${esc(fmtDur(t.duration_s))}</dd>
        <dt>Sandbox</dt><dd class="ui-mono">${esc(t.sandbox_branch || '—')}</dd>
        <dt>Source</dt><dd>${esc(t.source || '—')}</dd>
      </dl>`
      + (t.attempts.length ? `<div class="office-sub">Attempts (failover trail)</div><ol class="office-trail">${t.attempts.map((a, i) => `<li>${i + 1}. ${esc(a)}</li>`).join('')}</ol>` : '')
      + (flows.length ? `<div class="office-sub">Recent movements</div><ul class="office-trail">${flows.map(flowLine).join('')}</ul>` : '');
  }

  function panelAgent(a) {
    const tasks = S.state.tasks.filter((t) => t.agent === a.id).slice(0, 8);
    const cur = a.current_task_id ? S.state.tasks.find((t) => t.id === a.current_task_id) : tasks.find((t) => t.stage === 'running');
    const flows = S.state.flows.filter((f) => f.from === a.id || f.to === a.id).slice(0, 6);
    return head('Agent', a.label)
      + `<div class="office-badges">${ui.badge(a.status)}${a.derived ? '<span class="ops-chip" title="Seen on a task but not in the agent registry">Unregistered</span>' : ''}</div>
      <dl class="office-kv">
        <dt>ID</dt><dd class="ui-mono">${esc(a.id)}</dd>
        <dt>Kind</dt><dd>${esc((KINDS[a.kind] || KINDS.other).name)}</dd>
        <dt>Account</dt><dd class="ui-mono">${esc(a.account || '—')}</dd>
        <dt>Model</dt><dd class="ui-mono">${esc(a.model || '—')}</dd>
        <dt>Source</dt><dd>${esc(a.source || '—')}</dd>
      </dl>`
      + `<div class="office-sub">Current task</div>` + (cur ? `<div class="office-list">${taskLink(cur)}</div>` : '<p class="office-p">Not working on anything right now.</p>')
      + (tasks.length ? `<div class="office-sub">Recent tasks</div><div class="office-list">${tasks.map(taskLink).join('')}</div>` : '')
      + (flows.length ? `<div class="office-sub">Recent movements</div><ul class="office-trail">${flows.map(flowLine).join('')}</ul>` : '');
  }

  const STATION_INFO = {
    'station:inbox': ['Inbox', 'New tasks wait here until the router picks them up.', ['queued']],
    'station:router': ['Router', 'The router scores every agent for the task, picks the best fit and asks the model policy for a capability tier and model.', ['routing']],
    'station:done': ['Done shelf', 'Finished work waits here for review. Headless agents commit to a throwaway brain/swarm/<task> branch, so you can diff or discard it.', ['done', 'review']],
    'station:esc': ['Escalations', 'Tasks an agent could not finish. The architect agent analyses the blocker and either fixes it or records a decision, then the worker is unblocked.', ['escalated']],
  };

  function panelStation(key) {
    const info = STATION_INFO[key];
    const tasks = S.state.tasks.filter((t) => info[2].includes(t.stage));
    let extra = '';
    if (key === 'station:router' && S.decision) {
      const d = S.decision;
      extra = `<div class="office-sub">Latest decision</div><dl class="office-kv"><dt>Agent</dt><dd>${agentLink(d.agent)}</dd>
        <dt>Confidence</dt><dd>${confHtml(d.confidence)}</dd><dt>Tier</dt><dd>${tierChip(d.tier) || '—'}</dd>
        <dt>Model</dt><dd class="ui-mono">${esc(d.model || '—')}</dd></dl>`;
    }
    return head('Station', info[0]) + `<p class="office-p">${esc(info[1])}</p>` + extra
      + `<div class="office-sub">${tasks.length} task${tasks.length === 1 ? '' : 's'} here</div>`
      + (tasks.length ? `<div class="office-list">${tasks.slice(0, 20).map(taskLink).join('')}</div>` : '<p class="office-p">Empty right now.</p>');
  }

  function panelMemory() {
    const m = S.state.memory;
    return head('Memory cabinet', `${m.entries} entries`)
      + '<p class="office-p">Shared memory every agent reads and writes. When an agent finishes, what it learned is filed here so the next agent starts from it instead of from scratch.</p>'
      + '<div class="office-sub">Recent writes</div>'
      + (m.recent.length ? `<ul class="office-trail">${m.recent.slice(0, 10).map((r) => `<li>${esc(r.agent || 'unknown agent')}${r.scope ? ` <span class="ui-muted">· ${esc(r.scope)}</span>` : ''}<div class="ui-muted">${ui.timeTag(r.ts)}</div></li>`).join('')}</ul>` : '<p class="office-p">No recent writes.</p>');
  }

  function panelHandoff() {
    const h = S.state.handoff;
    return head('Handoff board', h.title || 'No baton in flight')
      + '<p class="office-p">The live baton: the task in flight and its exact next step, so any agent can resume where the last one stopped.</p>'
      + `<dl class="office-kv"><dt>Status</dt><dd>${h.status ? ui.badge(h.status) : '—'}</dd>
        <dt>Last agent</dt><dd>${esc(h.agent || '—')}</dd><dt>Next step</dt><dd>${esc(h.next || '—')}</dd>
        <dt>Updated</dt><dd>${ui.timeTag(h.updated_at)}</dd></dl>`;
  }

  function panelDefault() {
    const st = S.state;
    const running = st.tasks.filter((t) => t.stage === 'running' || t.stage === 'routing' || t.stage === 'delivered').slice(0, 8);
    return `<div class="office-panel-head"><div><div class="office-eyebrow">Details</div><h3 class="office-panel-title">Pick something on the floor</h3></div></div>
      <p class="office-p">Click an agent, a paper card or a station (Inbox, Router, Done, Escalations, Memory, Handoff board). With a keyboard, press Tab to move between them and Enter to open one.</p>
      <div class="office-sub">In motion now</div>`
      + (running.length ? `<div class="office-list">${running.map(taskLink).join('')}</div>` : '<p class="office-p">Nothing is running right now.</p>');
  }

  function refreshSelection() {
    const sel = S.selection;
    if (!sel || !S.state) return;
    if (sel.type === 'task') { const t = S.state.tasks.find((x) => x.id === sel.id); if (t) sel.data = t; else sel.gone = true; }
    if (sel.type === 'agent') { const d = S.deskById.get(sel.id); if (d) sel.data = d.agent; else sel.gone = true; }
  }

  function renderPanel() {
    const el = S.els.panel;
    if (!S.state) return;
    const sel = S.selection;
    let html;
    if (!sel) html = panelDefault();
    else if (sel.type === 'task') html = (sel.gone ? '<p class="office-p ui-muted">This task is no longer in the recent list; showing the last known state.</p>' : '') + panelTask(sel.data);
    else if (sel.type === 'agent') html = (sel.gone ? '<p class="office-p ui-muted">This agent left the floor; showing the last known state.</p>' : '') + panelAgent(sel.data);
    else if (sel.type === 'station') html = panelStation(sel.key);
    else if (sel.key === 'memory') html = panelMemory();
    else html = panelHandoff();
    if (html === S.panelHtml) return;
    S.panelHtml = html;
    el.innerHTML = html;
  }

  function select(key, fromKeyboard) {
    if (!key) { S.selection = null; }
    else {
      const i = key.indexOf(':');
      const type = i > 0 ? key.slice(0, i) : key;
      const id = i > 0 ? key.slice(i + 1) : '';
      if (type === 'task') { const t = S.state.tasks.find((x) => x.id === id); if (!t) return; S.selection = { type, id, key, data: t }; }
      else if (type === 'agent') { const d = S.deskById.get(id); if (!d) return; S.selection = { type, id, key, data: d.agent }; }
      else if (type === 'station' && STATION_INFO[key]) S.selection = { type, key };
      else if (key === 'memory' || key === 'handoff') S.selection = { type: key, key };
      else return;
    }
    renderPanel();
    requestDraw();
    // Below the side-by-side breakpoint the panel sits under the floor: bring
    // it into view when it is off screen (always for keyboard users).
    // Scroll the shell's own scroller (#main-content), never the document:
    // scrollIntoView() would also shift the fixed-height page shell.
    const panel = S.els.panel;
    const scroller = document.getElementById('main-content');
    if (key && panel && scroller && scroller.contains(panel) && window.matchMedia('(max-width: 1535.98px)').matches) {
      const pr = panel.getBoundingClientRect(), sr = scroller.getBoundingClientRect();
      const below = pr.top > sr.bottom - 60;
      if (fromKeyboard || below) {
        const delta = pr.height > sr.height - 32 || pr.top < sr.top ? pr.top - sr.top - 16 : Math.max(0, pr.bottom - sr.bottom + 16);
        if (Math.abs(delta) > 1) scroller.scrollBy({ top: delta, behavior: motionOn() ? 'smooth' : 'auto' });
      }
    }
  }

  function syncHotspots(force) {
    const L = S.layout, box = S.els.hotspots;
    if (!L || !box) return;
    const n = counts();
    const items = [
      ['station:inbox', L.inbox, `Inbox: ${n.queued} queued`],
      ['station:router', L.router, 'Router' + (S.decision ? `: last sent a task to ${agentLabel(S.decision.agent)}` : '')],
    ];
    for (const d of S.desks) {
      const a = d.agent;
      const task = a.status === 'working' ? S.state.tasks.find((x) => x.id === a.current_task_id || (x.agent === a.id && x.stage === 'running')) : null;
      items.push(['agent:' + a.id, agentRect(d), `${a.label}, ${a.status}${task ? `, working on: ${task.title}` : ''}`]);
    }
    items.push(['station:done', L.done, `Done shelf: ${n.done} tasks`], ['station:esc', L.esc, `Escalations: ${n.escalated} tasks`],
      ['memory', L.memory, `Memory cabinet: ${n.memory} entries`], ['handoff', L.board, 'Handoff board' + (S.state.handoff.next ? `, next: ${S.state.handoff.next}` : '')]);
    const sig = items.map((i) => i[0] + '|' + i[2] + '|' + [i[1].x, i[1].y, i[1].w, i[1].h].join(',')).join('\n') + '|' + L.W + 'x' + L.H;
    if (!force && sig === S.hotSig) return;
    S.hotSig = sig;
    const focused = document.activeElement && box.contains(document.activeElement) ? document.activeElement.dataset.key : null;
    box.textContent = '';
    for (const [key, r, label] of items) {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'office-hotspot';
      btn.dataset.key = key;
      btn.setAttribute('aria-label', label);
      btn.style.left = (r.x / L.W * 100) + '%';
      btn.style.top = (r.y / L.H * 100) + '%';
      btn.style.width = (r.w / L.W * 100) + '%';
      btn.style.height = (r.h / L.H * 100) + '%';
      btn.addEventListener('click', () => select(key, true));
      box.appendChild(btn);
      if (focused === key) btn.focus({ preventScroll: true });
    }
  }

  function renderSr() {
    const st = S.state;
    const lines = [summaryText()];
    const agents = S.desks.map((d) => {
      const a = d.agent;
      const task = a.status === 'working' ? st.tasks.find((x) => x.id === a.current_task_id || (x.agent === a.id && x.stage === 'running')) : null;
      return `${a.label}: ${a.status}${task ? `, working on ${task.title}` : ''}`;
    });
    const tasks = st.tasks.slice(0, 20).map((t) => `${t.title}: ${t.stage}${t.agent ? `, ${agentLabel(t.agent)}` : ''}`);
    const sig = lines.concat(agents, tasks).join('\n');
    if (sig === S.srSig) return;
    S.srSig = sig;
    S.els.sr.innerHTML = `<p>${esc(lines[0])}</p><h3>Agents</h3><ul>${agents.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`
      + `<h3>Recent tasks</h3><ul>${tasks.map((x) => `<li>${esc(x)}</li>`).join('')}</ul>`;
  }

  // ── Data: polling, SSE nudges, demo fallback ──────────────────────────────
  function resetScene() {
    S.cards.clear(); S.flyers.length = 0; S.seenFlows.clear(); S.seats.clear();
    S.state = null; S.selection = null; S.panelHtml = ''; S.srSig = ''; S.hotSig = ''; S.statsSig = ''; S.layoutKey = '';
  }

  function enterLive(st) {
    const switching = S.mode !== 'live';
    if (switching) resetScene();
    S.mode = 'live'; S.demo = null; S.demoReason = ''; S.fetchNote = '';
    S.lastOkAt = Date.now();
    showBanner(null);
    applySnapshot(st, { reset: switching });
  }

  function enterDemo(reason) {
    S.demoReason = reason;
    if (S.mode !== 'demo') {
      resetScene();
      S.mode = 'demo';
      S.demo = createDemo();
      applySnapshot(S.demo.snapshot(), { reset: true });
    }
    showBanner(reason);
    updateStatus();
  }

  function onFetchFail(reason, note) {
    S.fetchNote = note;
    if (S.mode === 'live') { updateStatus(); return; }
    enterDemo(reason);
  }

  async function fetchState() {
    if (S.inFlight) return;
    S.inFlight = true;
    S.lastFetchAt = Date.now();
    S.stats.fetches++;
    try {
      const doFetch = typeof window.fetchWithAuth === 'function' ? window.fetchWithAuth : (u) => fetch(u, { credentials: 'same-origin' });
      let res;
      try { res = await doFetch(API_URL); } catch (e) { onFetchFail('error', 'offline'); return; }
      if (res.status === 404 || res.status === 501) { S.probeAfter = Date.now() + PROBE_MS; onFetchFail('endpoint', ''); return; }
      if (res.status === 401 || res.status === 403) { onFetchFail('auth', 'Sign-in needed'); return; }
      if (!res.ok) { onFetchFail('error', `HTTP ${res.status}`); return; }
      let data;
      try { data = await res.json(); } catch (e) { onFetchFail('error', 'unreadable response'); return; }
      const st = normalize(data);
      if (isEmptyState(st)) { S.fetchNote = ''; S.lastOkAt = Date.now(); enterDemo('empty'); return; }
      enterLive(st);
    } finally {
      S.inFlight = false;
    }
  }

  function schedule() {
    clearTimeout(S.timer);
    if (S.active) S.timer = setTimeout(tick, TICK_MS);
  }

  async function tick() {
    if (!S.active) return;
    if (S.mode === 'demo' && S.demo) { S.demo.step(); applySnapshot(S.demo.snapshot(), {}); }
    const now = Date.now();
    if (now - S.lastFetchAt >= POLL_MS - 100 && now >= S.probeAfter) await fetchState();
    updateStatus();
    schedule();
  }

  // Called by the dashboard's SSE handler: fetch soon after task/job/routing
  // events instead of waiting for the next poll.
  function onEvent(evt) {
    if (!S.active || !evt) return;
    const type = String(evt.event_type || evt.type || '').toUpperCase();
    if (!/^(TASK|SWARM|JOB|ROUT|MEMORY|HANDOFF|AGENT)/.test(type)) return;
    if (S.mode === 'demo' && S.demoReason === 'endpoint') return;
    clearTimeout(S.eventTimer);
    S.eventTimer = setTimeout(() => { if (S.active && Date.now() - S.lastFetchAt > 800) fetchState(); }, 250);
  }

  function refresh() {
    S.probeAfter = 0;
    return fetchState();
  }

  // ── Demo simulation (client-side, same contract as the real endpoint) ────
  function createDemo() {
    let seed = 0x5eed1234;
    const rnd = () => { seed = (seed + 0x6D2B79F5) | 0; let x = Math.imul(seed ^ (seed >>> 15), 1 | seed); x = (x + Math.imul(x ^ (x >>> 7), 61 | x)) ^ x; return ((x ^ (x >>> 14)) >>> 0) / 4294967296; };
    const pick = (list) => list[Math.floor(rnd() * list.length)];
    const iso = (ms) => new Date(ms).toISOString();
    const agents = [
      { id: 'kiro-cli', label: 'Kiro CLI', kind: 'kiro-cli', account: 'kiro-default', model: 'auto' },
      { id: 'cline', label: 'Cline', kind: 'cline', account: 'cline-default', model: 'auto' },
      { id: 'antigravity', label: 'Antigravity', kind: 'antigravity', account: 'antigravity', model: 'gemini-3.7-flash-high' },
      { id: 'antigravity-account-2', label: 'Antigravity · key', kind: 'antigravity-api', account: 'antigravity-account-2', model: 'gemini-3.7-flash-medium' },
      { id: 'antigravity-ide', label: 'Antigravity IDE', kind: 'antigravity-ide', account: null, model: null },
      { id: 'openhands', label: 'OpenHands', kind: 'openhands', account: 'openhands-local', model: null },
      { id: 'api-worker', label: 'API worker', kind: 'api', account: 'api-key-1', model: 'gemini-3.8-flash-low' },
    ].map((a) => Object.assign({ status: 'idle', current_task_id: null, source: 'mission-control', busy: 0, blockedLeft: 0 }, a));
    agents.find((a) => a.id === 'openhands').status = 'offline';
    const byId = (id) => agents.find((a) => a.id === id);
    const jobs = [
      ['Run the integration test suite and report failures', 'kiro-cli', 'fast', 'Terminal and test work fits the CLI workhorse.'],
      ['Check pod health in the staging namespace', 'kiro-cli', 'fast', 'Shell and cluster commands route to the terminal agent.'],
      ['Refactor modal styling to Tailwind utility classes', 'cline', 'balanced', 'A focused change in one or two files.'],
      ['Fix the flaky retry test in the job runner', 'cline', 'balanced', 'Single-file bug fix with a test.'],
      ['Add docstrings to the routing module', 'api-worker', 'fast', 'Mechanical edit; the cheapest capable worker is enough.'],
      ['Split the auth guards into separate modules', 'antigravity', 'advanced', 'Spans many files: multi-file refactor.'],
      ['Design a database schema for the audit log', 'antigravity', 'frontier', 'Architecture and deep reasoning.'],
      ['Draft an ADR for the sandbox cleanup policy', 'antigravity-account-2', 'balanced', 'Reasoning task; second account has spare quota.'],
      ['Polish the settings page layout in the editor', 'antigravity-ide', 'balanced', 'Requested for the IDE by name; delivered for a human to drive.'],
      ['Write a runbook for rotating the API key', 'antigravity-account-2', 'balanced', 'Documentation with light reasoning.'],
      ['Profile the memory search endpoint', 'kiro-cli', 'balanced', 'Needs a shell to run the profiler.'],
      ['Summarise yesterday’s escalations for the team', 'api-worker', 'fast', 'Short summary; low-cost tier.'],
    ];
    const tasks = [];
    let flows = [];
    let n = 0;
    let memory = 42;
    const memRecent = [];
    let handoff = { title: 'Wire the office view into Mission Control', status: 'in-progress', agent: 'antigravity', next: 'Review the diff on the sandbox branch, then merge', updated_at: iso(Date.now() - 600000) };
    const flow = (type, task, from, to, detail, at) => { flows.unshift({ ts: iso(at || Date.now()), type, task_id: task ? task.id : null, from, to, detail }); flows = flows.slice(0, 100); };
    const modelFor = (agent, tier) => (byId(agent).model || (tier === 'frontier' ? 'recommended: frontier tier' : null));
    function newTask(at) {
      const j = jobs[n % jobs.length];
      n++;
      const t = { id: 'task-demo' + String(n).padStart(3, '0'), title: j[0], stage: 'queued', agent: null, kind: null, model: null, model_tier: null,
        complexity: j[2] === 'frontier' || j[2] === 'advanced' ? 'high' : (j[2] === 'fast' ? 'low' : 'medium'), risk: j[2] === 'frontier' ? 'medium' : 'low',
        routing_reason: null, confidence: null, requires_approval: j[1] === 'antigravity-ide', created_at: iso(at), started_at: null, completed_at: null,
        duration_s: null, sandbox_branch: null, attempts: [], source: 'swarm', _plan: j, _left: 0, _age: 0 };
      tasks.unshift(t);
      flow('dispatch', t, 'dispatch', 'inbox', 'queued from the dispatch box', at);
      return t;
    }
    function route(t, at) {
      const [, agent, tier, why] = t._plan;
      let target = agent;
      if (byId(target).status === 'offline') target = 'cline';
      Object.assign(t, { stage: 'routing', agent: target, kind: byId(target).kind, model_tier: tier, model: modelFor(target, tier), routing_reason: why, confidence: Math.round((0.62 + rnd() * 0.33) * 100) / 100 });
      flow('route', t, 'router', target, `scored ${agents.length} agents; ${tier} tier`, at);
    }
    function start(t, at) {
      const a = byId(t.agent);
      if (a.kind === 'antigravity-ide') {
        t.stage = 'delivered'; t._left = 6 + Math.floor(rnd() * 4);
        flow('deliver', t, 'router', a.id, 'delivered to the IDE; waits for a human', at);
        return true;
      }
      if (a.status !== 'idle') return false;
      Object.assign(t, { stage: 'running', started_at: iso(at), sandbox_branch: 'brain/swarm/' + t.id });
      t.attempts = [a.account || a.id];
      t._left = 3 + Math.floor(rnd() * 4);
      a.status = 'working'; a.current_task_id = t.id;
      flow('start', t, 'router', a.id, 'started in a git worktree sandbox', at);
      return true;
    }
    function finish(t, at, outcome) {
      const a = byId(t.agent);
      if (a.current_task_id === t.id) { a.status = 'idle'; a.current_task_id = null; }
      t.completed_at = iso(at);
      t.duration_s = t.started_at ? Math.max(1, Math.round((at - Date.parse(t.started_at)) / 1000)) : null;
      if (outcome === 'escalated') {
        t.stage = 'escalated';
        flow('escalate', t, a.id, 'escalations', 'blocked: tests need a secret this sandbox does not have', at);
      } else {
        t.stage = 'done';
        flow('finish', t, a.id, 'done', 'finished; changes committed to the sandbox branch', at);
        memory++;
        memRecent.unshift({ ts: iso(at), agent: a.id, scope: 'project' });
        memRecent.splice(10);
        flow('memory_write', t, a.id, 'memory', 'saved what it learned', at + 1);
        if (rnd() < 0.35) {
          handoff = { title: t.title, status: 'done', agent: a.id, next: 'Review brain/swarm/' + t.id + ' and merge or discard it', updated_at: iso(at) };
          flow('handoff', t, a.id, 'handoff', 'checkpoint written', at + 2);
        }
      }
    }
    // Seed a lived-in floor: finished history plus work in flight.
    const t0 = Date.now() - 900000;
    for (let i = 0; i < 7; i++) {
      const at = t0 + i * 90000;
      const t = newTask(at); route(t, at + 1000);
      if (t.agent === 'antigravity-ide') { t.stage = 'done'; t.completed_at = iso(at + 60000); continue; }
      start(t, at + 2000); finish(t, at + 40000 + i * 3000, i === 3 ? 'escalated' : 'done');
    }
    for (const id of ['kiro-cli', 'antigravity']) {
      const t = newTask(Date.now() - 60000);
      t._plan = jobs.find((j) => j[1] === id);
      t.title = t._plan[0];
      route(t, Date.now() - 58000); start(t, Date.now() - 50000);
    }
    const ide = newTask(Date.now() - 40000); ide._plan = jobs[8]; ide.title = jobs[8][0]; ide.requires_approval = true; route(ide, Date.now() - 39000); start(ide, Date.now() - 38000);
    newTask(Date.now() - 5000); newTask(Date.now() - 2000);
    let step = 0;

    return {
      step() {
        step++;
        const at = Date.now();
        const queued = tasks.filter((t) => t.stage === 'queued');
        if ((step % 3 === 0 || rnd() < 0.2) && queued.length < 4) newTask(at);
        const routing = tasks.filter((t) => t.stage === 'routing');
        for (const t of routing) { if (t._age++ >= 1) start(t, at); }
        const oldest = queued[queued.length - 1];
        if (oldest && routing.length < 2 && step % 2 === 0) route(oldest, at);
        for (const a of agents) {
          if (a.status === 'blocked' && --a.blockedLeft <= 0) {
            const t = tasks.find((x) => x.id === a.current_task_id);
            a.status = 'working';
            if (t) finish(t, at, 'escalated');
          }
        }
        for (const t of tasks.filter((x) => x.stage === 'running')) {
          const a = byId(t.agent);
          if (a.status === 'blocked') continue;
          if (--t._left > 0) continue;
          const roll = rnd();
          if (roll < 0.14) { a.status = 'blocked'; a.blockedLeft = 3; continue; }
          if (roll < 0.3 && (a.kind === 'antigravity' || a.kind === 'antigravity-api')) {
            const other = a.id === 'antigravity' ? byId('antigravity-account-2') : byId('antigravity');
            if (other.status === 'idle') {
              a.status = 'idle'; a.current_task_id = null;
              Object.assign(t, { agent: other.id, kind: other.kind, model: other.model, _left: 3 });
              t.attempts.push(other.account || other.id);
              other.status = 'working'; other.current_task_id = t.id;
              flow('failover', t, a.id, other.id, 'RESOURCE_EXHAUSTED on ' + (a.account || a.id) + '; failing over', at);
              continue;
            }
          }
          finish(t, at, 'done');
        }
        for (const t of tasks.filter((x) => x.stage === 'delivered')) {
          if (--t._left <= 0) { t.stage = 'done'; t.completed_at = iso(at); flow('finish', t, t.agent, 'done', 'completed by a human in the IDE', at); }
        }
        // Keep the list short, the way the endpoint caps it.
        const keep = tasks.filter((t) => t.stage !== 'done' && t.stage !== 'escalated');
        const doneish = tasks.filter((t) => t.stage === 'done' || t.stage === 'escalated').slice(0, 24);
        tasks.length = 0;
        tasks.push(...keep, ...doneish);
        tasks.sort((a, b) => Date.parse(b.created_at) - Date.parse(a.created_at));
      },
      snapshot() {
        const clean = (t) => { const o = {}; for (const k of Object.keys(t)) if (k[0] !== '_') o[k] = Array.isArray(t[k]) ? t[k].slice() : t[k]; return o; };
        return normalize({
          generated_at: iso(Date.now()),
          sources: { mission_control: true, brain_swarm: true, brain_dir: null },
          agents: agents.map((a) => ({ id: a.id, label: a.label, kind: a.kind, account: a.account, status: a.status, current_task_id: a.current_task_id, model: a.model, source: a.source })),
          tasks: tasks.map(clean), flows: flows.slice(),
          memory: { entries: memory, recent: memRecent.slice() },
          handoff: Object.assign({}, handoff),
        });
      },
    };
  }

  // ── Wiring ────────────────────────────────────────────────────────────────
  function isVisible() {
    const sec = S.els && S.els.section;
    return !!sec && !sec.classList.contains('hidden') && !document.hidden;
  }

  function syncActive() {
    const on = isVisible();
    if (on === S.active) return;
    S.active = on;
    if (on) {
      onResize();
      if (!S.layout) ensureLayout(7);
      requestDraw();
      if (S.mode === 'demo' || Date.now() - S.lastFetchAt >= 1000) {
        if (S.mode !== 'demo' || S.demoReason !== 'endpoint' || Date.now() >= S.probeAfter) fetchState().finally(schedule);
        else schedule();
      } else schedule();
    } else {
      clearTimeout(S.timer); clearTimeout(S.eventTimer);
      if (S.raf) cancelAnimationFrame(S.raf);
      S.raf = 0;
    }
  }

  function onCanvasPointer(e, click) {
    const L = S.layout;
    if (!L) return;
    const rect = S.canvas.getBoundingClientRect();
    if (!rect.width) return;
    const x = (e.clientX - rect.left) / rect.width * L.W;
    const y = (e.clientY - rect.top) / rect.height * L.H;
    let found = null;
    for (let i = S.hits.length - 1; i >= 0; i--) {
      const h = S.hits[i];
      if (x >= h.x && x < h.x + h.w && y >= h.y && y < h.y + h.h) { found = h.key; break; }
    }
    if (click) select(found, false);
    else S.canvas.style.cursor = found ? 'pointer' : 'default';
  }

  function applyMotionButton() {
    const btn = S.els.motionBtn;
    if (!btn) return;
    const reduced = S.reducedMQ && S.reducedMQ.matches;
    btn.disabled = !!reduced;
    btn.setAttribute('aria-pressed', S.motionPaused || reduced ? 'true' : 'false');
    btn.innerHTML = reduced
      ? '<i class="fa-solid fa-pause" aria-hidden="true"></i> Motion off (system setting)'
      : (S.motionPaused ? '<i class="fa-solid fa-play" aria-hidden="true"></i> Resume motion' : '<i class="fa-solid fa-pause" aria-hidden="true"></i> Pause motion');
  }

  function onMotionChange() {
    applyMotionButton();
    if (!motionOn()) {
      for (const card of S.cards.values()) { card.path = []; card.seg = null; if (card.target) { card.x = card.target.x; card.y = card.target.y; } }
      S.flyers.length = 0;
    }
    if (S.raf) { cancelAnimationFrame(S.raf); S.raf = 0; }
    requestDraw();
  }

  function init() {
    const $ = (id) => document.getElementById(id);
    const section = $('tab-office');
    const canvas = $('office-canvas');
    if (!section || !canvas || !ui) return;
    S.els = {
      section, canvas, stage: $('office-stage'), hotspots: $('office-hotspots'), panel: $('office-panel'),
      banner: $('office-banner'), stats: $('office-stats'), sr: $('office-sr'),
      status: $('office-status'), statusText: $('office-status-text'), motionBtn: $('office-motion-btn'), refreshBtn: $('office-refresh-btn'),
    };
    S.canvas = canvas;
    S.ctx = canvas.getContext('2d');
    S.reducedMQ = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;
    try { S.motionPaused = localStorage.getItem('mc.office.motion') === 'off'; } catch (e) { /* storage blocked */ }
    applyMotionButton();
    if (S.reducedMQ && S.reducedMQ.addEventListener) S.reducedMQ.addEventListener('change', onMotionChange);

    canvas.addEventListener('click', (e) => onCanvasPointer(e, true));
    let moveQueued = false;
    canvas.addEventListener('mousemove', (e) => {
      if (moveQueued) return;
      moveQueued = true;
      requestAnimationFrame(() => { moveQueued = false; onCanvasPointer(e, false); });
    });
    S.els.panel.addEventListener('click', (e) => {
      const btn = e.target.closest('[data-office-select], [data-office-close]');
      if (!btn) return;
      if (btn.hasAttribute('data-office-close')) { select(null); return; }
      select(btn.getAttribute('data-office-select'), true);
    });
    if (S.els.motionBtn) S.els.motionBtn.addEventListener('click', () => {
      S.motionPaused = !S.motionPaused;
      try { localStorage.setItem('mc.office.motion', S.motionPaused ? 'off' : 'on'); } catch (e) { /* storage blocked */ }
      onMotionChange();
    });
    if (S.els.refreshBtn) S.els.refreshBtn.addEventListener('click', () => ui.busy(S.els.refreshBtn, refresh));

    if (window.ResizeObserver) new ResizeObserver(() => onResize()).observe(S.els.stage);
    else window.addEventListener('resize', onResize);
    new MutationObserver(syncActive).observe(section, { attributes: true, attributeFilter: ['class'] });
    document.addEventListener('visibilitychange', syncActive);
    syncActive();
  }

  window.MCOffice = {
    onEvent, refresh,
    // Read-only hooks for tests and debugging.
    debug: { stats: S.stats, mode: () => S.mode, active: () => S.active, state: () => S.state, summary: () => (S.state ? summaryText() : '') },
  };

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
