// UI E2E for the Shared Brain dashboard: the knowledge graph, the handoff baton
// panel and the Swarm Radar. These are the surfaces an operator reads to decide
// what an agent is doing, so "renders with real data" is the thing under test.
const { test, expect } = require('@playwright/test');

test.describe('dashboard shell', () => {
  test('loads without console or page errors', async ({ page }) => {
    const problems = [];
    page.on('console', (m) => {
      if (m.type() === 'error') problems.push(`console: ${m.text()}`);
    });
    page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));

    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await expect(page).toHaveTitle(/Shared Brain/);

    // Ignore favicon noise, which is not a functional failure.
    const real = problems.filter((p) => !/favicon/i.test(p));
    expect(real, `page reported errors:\n${real.join('\n')}`).toHaveLength(0);
  });

  test('serves its vendored static assets, so the UI is not CDN-dependent', async ({ request }) => {
    for (const asset of [
      '/static/tailwind.js',
      '/static/force-graph.min.js',
      '/static/marked.min.js',
    ]) {
      const res = await request.get(asset);
      expect(res.status(), `${asset} not served`).toBe(200);
      expect((await res.body()).length, `${asset} is empty`).toBeGreaterThan(1000);
    }
  });
});

test.describe('handoff baton panel', () => {
  test('renders the live baton with a title and a next action', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });

    const title = page.locator('#baton-title');
    await expect(title).toBeVisible();
    await expect(title).not.toHaveText(/^\s*$/);

    // [next] is the field that makes a checkpoint useful to the next agent, so the
    // panel must actually show it rather than leaving a placeholder.
    const next = page.locator('#baton-next');
    await expect(next).toBeVisible();
    const nextText = (await next.innerText()).trim();
    expect(nextText.length, 'baton-next is empty').toBeGreaterThan(0);
    expect(nextText).not.toMatch(/^(--|n\/a|loading)$/i);

    await expect(page.locator('#baton-agent-badge')).not.toHaveText(/^\s*$/);
  });
});

test.describe('knowledge graph', () => {
  test('counters show a non-zero store and match the notes API', async ({ page, request }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });

    const countAll = page.locator('#count-all');
    await expect(countAll).toBeVisible();
    await expect(countAll).toHaveText(/\d+/, { timeout: 20_000 });
    const shown = parseInt((await countAll.innerText()).replace(/\D/g, ''), 10);
    expect(shown).toBeGreaterThan(0);

    const apiNotes = await (await request.get('/api/notes')).json();
    const notes = Array.isArray(apiNotes) ? apiNotes : apiNotes.notes;
    expect(shown, 'UI note count disagrees with /api/notes').toBe(notes.length);

    // Handoffs and decisions both exist in this store, so their counters must be > 0.
    for (const id of ['#count-handoffs', '#count-decisions']) {
      const n = parseInt((await page.locator(id).innerText()).replace(/\D/g, ''), 10);
      expect(n, `${id} is zero`).toBeGreaterThan(0);
    }
  });

  test('renders the force graph onto a canvas', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const canvas = page.locator('canvas').first();
    await expect(canvas).toBeVisible({ timeout: 20_000 });

    // A collapsed canvas means force-graph never received a size.
    const box = await canvas.boundingBox();
    expect(box.width).toBeGreaterThan(100);
    expect(box.height).toBeGreaterThan(100);

    // And it must actually paint, not just exist.
    const painted = await page.evaluate(() => {
      const c = document.querySelector('canvas');
      if (!c) return false;
      const ctx = c.getContext('2d');
      const { data } = ctx.getImageData(0, 0, c.width, Math.min(c.height, 400));
      for (let i = 3; i < data.length; i += 4) if (data[i] !== 0) return true;
      return false;
    });
    expect(painted, 'graph canvas has no painted pixels').toBe(true);
  });

  test('search highlights matching graph nodes and clearing resets the selection', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });

    const search = page.locator('#quick-search');
    await expect(search).toBeVisible();
    const clearBtn = page.locator('#clear-search');

    // Wait for the graph payload to land, otherwise there is nothing to match against.
    await expect
      .poll(async () => page.evaluate(() => (rawGraphData?.nodes || []).length), { timeout: 20_000 })
      .toBeGreaterThan(0);

    // The clear affordance is hidden until there is a query to clear.
    await expect(clearBtn).toBeHidden();

    // A term that exists in the store must highlight at least one node, and exactly
    // as many as the handler's own match rule (name/path/type/agent substring).
    await search.fill('handoff');
    const { matched, expected } = await page.evaluate(() => {
      const q = 'handoff';
      const expected = (rawGraphData?.nodes || []).filter(
        (n) =>
          (n.name || '').toLowerCase().includes(q) ||
          (n.path || '').toLowerCase().includes(q) ||
          (n.type || '').toLowerCase().includes(q) ||
          (n.agent || '').toLowerCase().includes(q),
      ).length;
      return { matched: searchMatchingNodeIds.size, expected };
    });
    expect(matched).toBeGreaterThan(0);
    expect(matched, 'highlight set disagrees with the handler match rule').toBe(expected);
    await expect(clearBtn).toBeVisible();

    // A term matching nothing must highlight nothing rather than falling back to all.
    await search.fill('zzz-no-such-note-zzz');
    await expect
      .poll(async () => page.evaluate(() => searchMatchingNodeIds.size), { timeout: 10_000 })
      .toBe(0);

    // Clicking clear must empty the input, hide itself, and drop the highlight set.
    await clearBtn.click();
    await expect(search).toHaveValue('');
    await expect(clearBtn).toBeHidden();
    expect(await page.evaluate(() => searchMatchingNodeIds.size)).toBe(0);
  });
});

test.describe('swarm radar', () => {
  test('shows every lifecycle column', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    for (const bucket of ['pending', 'inprogress', 'completed', 'escalated']) {
      await expect(page.locator(`#swarm-list-${bucket}`), `#swarm-list-${bucket} missing`).toBeAttached();
    }
  });

  test('completed column is populated from the real queue', async ({ page, request }) => {
    const snap = await (await request.get('/api/swarm')).json();
    const completed =
      snap.completed ?? snap.tasks?.completed ?? snap.queue?.completed ?? [];
    test.skip(!Array.isArray(completed) || completed.length === 0, 'no completed tasks in the queue yet');

    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const column = page.locator('#swarm-list-completed');
    await expect(column).toBeAttached();
    await expect
      .poll(async () => (await column.innerText()).trim().length, { timeout: 20_000 })
      .toBeGreaterThan(0);
  });

  test('exposes the dispatch and heal controls', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    await expect(page.locator('#dispatch-swarm-btn')).toBeAttached();
    await expect(page.locator('#execute-heal-btn')).toBeAttached();
  });
});

test.describe('live updates', () => {
  test('/api/events serves an SSE stream', async ({ page }) => {
    await page.goto('/', { waitUntil: 'domcontentloaded' });
    const contentType = await page.evaluate(async () => {
      const res = await fetch('/api/events', { headers: { Accept: 'text/event-stream' } });
      const ct = res.headers.get('content-type') || '';
      // Release the stream so the server thread is not left hanging.
      try { res.body?.cancel(); } catch {}
      return `${res.status}|${ct}`;
    });
    expect(contentType).toMatch(/^200\|text\/event-stream/);
  });
});
