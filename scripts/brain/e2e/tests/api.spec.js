// API-level contract tests for the Shared Brain dashboard.
//
// These assert the shapes the UI actually consumes, so a regression in the Python
// handlers is caught here rather than as a blank panel in the browser.
const { test, expect } = require('@playwright/test');

test.describe('brain JSON API', () => {
  test('/api/status reports a healthy store with counts', async ({ request }) => {
    const res = await request.get('/api/status');
    expect(res.status()).toBe(200);
    const body = await res.json();

    // The store must be non-empty; a zero note count means the brain failed to load.
    expect(typeof body).toBe('object');
    const json = JSON.stringify(body);
    expect(json.length).toBeGreaterThan(2);
    // total_notes is what the header counter renders from.
    const total = body.total_notes ?? body.totalNotes ?? body.counts?.all;
    expect(total, `no note total in /api/status: ${json.slice(0, 300)}`).toBeGreaterThan(0);
  });

  test('/api/graph returns a connected knowledge graph', async ({ request }) => {
    const res = await request.get('/api/graph');
    expect(res.status()).toBe(200);
    const g = await res.json();

    expect(Array.isArray(g.nodes)).toBe(true);
    expect(Array.isArray(g.links)).toBe(true);
    expect(g.nodes.length).toBeGreaterThan(0);

    // Every node the force-graph renders needs an id, or the UI silently drops it.
    for (const n of g.nodes) expect(n.id, `node without id: ${JSON.stringify(n)}`).toBeTruthy();

    // Links must reference real nodes; a dangling link breaks force-graph layout.
    const ids = new Set(g.nodes.map((n) => n.id));
    const dangling = g.links.filter((l) => {
      const s = typeof l.source === 'object' ? l.source.id : l.source;
      const t = typeof l.target === 'object' ? l.target.id : l.target;
      return !ids.has(s) || !ids.has(t);
    });
    expect(dangling, `dangling links: ${JSON.stringify(dangling.slice(0, 5))}`).toHaveLength(0);
  });

  test('/api/notes lists notes including the live handoff baton', async ({ request }) => {
    const res = await request.get('/api/notes');
    expect(res.status()).toBe(200);
    const body = await res.json();
    const notes = Array.isArray(body) ? body : body.notes;
    expect(Array.isArray(notes)).toBe(true);
    expect(notes.length).toBeGreaterThan(0);

    const paths = notes.map((n) => n.path || n.rel_path || n.id || '');
    expect(
      paths.some((p) => p.includes('handoff/current')),
      'handoff/current missing from /api/notes — the baton is the one note that must always be listed',
    ).toBe(true);
  });

  test('/api/note returns parsed frontmatter and observations for the baton', async ({ request }) => {
    const res = await request.get('/api/note?path=handoff/current.md');
    expect(res.status()).toBe(200);
    const note = await res.json();

    expect(note.content || note.body, 'note has no content').toBeTruthy();
    // The handoff schema is enforced, so these observations must parse out.
    const text = JSON.stringify(note);
    for (const field of ['status', 'agent', 'next']) {
      expect(text, `observation [${field}] not surfaced by /api/note`).toContain(field);
    }
  });

  test('/api/swarm returns the queue snapshot with all lifecycle buckets', async ({ request }) => {
    const res = await request.get('/api/swarm');
    expect(res.status()).toBe(200);
    const snap = await res.json();
    const text = JSON.stringify(snap);
    for (const bucket of ['pending', 'completed', 'escalated']) {
      expect(text, `swarm snapshot missing "${bucket}"`).toContain(bucket);
    }
  });

  test('/api/providers never leaks a credential value', async ({ request }) => {
    const res = await request.get('/api/providers');
    // 503 is legitimate when the providers module is absent; anything else must be 200.
    expect([200, 503]).toContain(res.status());
    if (res.status() !== 200) return;

    const body = await res.json();
    expect(Array.isArray(body.accounts)).toBe(true);

    // Contract from the handler comment: list_accounts() never contains a key value.
    // Assert no field looks like a live secret.
    const walk = (node, path = '') => {
      if (node === null || node === undefined) return;
      if (typeof node === 'string') {
        expect(node, `suspected secret at ${path}`).not.toMatch(/^(sk-|ya29\.|AIza|aok_|ghp_)/);
        return;
      }
      if (Array.isArray(node)) return node.forEach((v, i) => walk(v, `${path}[${i}]`));
      if (typeof node === 'object') {
        for (const [k, v] of Object.entries(node)) {
          if (/(key|token|secret|password)$/i.test(k) && typeof v === 'string' && v) {
            // A masked placeholder is fine; a plausible raw key is not.
            expect(v.length, `${path}.${k} looks like a raw secret`).toBeLessThan(12);
          }
          walk(v, `${path}.${k}`);
        }
      }
    };
    walk(body);
  });
});

test.describe('brain API guard rails', () => {
  test('/api/note rejects a path traversal attempt outside the brain', async ({ request }) => {
    for (const evil of [
      '/api/note?path=../../../../etc/passwd',
      '/api/note?path=../.ssh/id_rsa',
      '/api/note?path=/etc/passwd',
    ]) {
      const res = await request.get(evil);
      expect(res.status(), `${evil} was not rejected`).toBe(404);
      const body = await res.text();
      expect(body).not.toContain('root:x:');
    }
  });

  test('/api/note without a path parameter is a 400, not a crash', async ({ request }) => {
    const res = await request.get('/api/note');
    expect(res.status()).toBe(400);
  });

  test('/api/swarm/dispatch refuses GET and directs the caller to POST', async ({ request }) => {
    const res = await request.get('/api/swarm/dispatch');
    expect(res.status()).toBe(405);
  });

  test('an unknown API route 404s instead of returning the HTML shell', async ({ request }) => {
    const res = await request.get('/api/definitely-not-a-route');
    expect(res.status()).toBe(404);
    expect(await res.text()).not.toContain('<title>Shared Brain');
  });
});
