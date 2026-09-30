# Handover: audit, bug-fix and UI pass on Mission Control

- **Written:** 2026-09-30 by Claude (Opus 5.5), Claude Code session in the desktop app
- **Repo:** `/home/setoo/YashDevops/Agentic_shared_memory` · branch `Dev/fix` (base `main`)
- **Status:** Audit stopped partway at the user's request. **No repo code has been changed yet.** The next model should finish the audit where needed, then run the fix phase.

---

## 1. What the user asked for

In the user's words:

1. "check all thing and make changes according and also make ui more user friendly"
2. "make sure every function should work nicely and there should be no bug, deploy more sub agents"
3. "stop the current task and create a handover .md file" (this file)

So the goal is to audit the whole project, fix the verified bugs, and make the Mission Control dashboard UI more user friendly. The user wants multiple subagents working in parallel and wants every function working.

**Do not commit or push unless the user asks.** The user has uncommitted work-in-progress (see §3).

---

## 2. Project in one paragraph

"Agentic Brain" is a stdlib-only Python 3.14 orchestrator. It routes coding tasks to several headless agent accounts: Antigravity accounts 1/2/3/2077/2078, `kiro-cli`, and Cline accounts 1/2/3. It also provides SQLite FTS5 shared memory, handoffs, git-worktree sandboxes, and usage/cost/quota tracking. The web UI is **Mission Control**:

| Piece | File | Notes |
|---|---|---|
| HTTP server + all `/api/*` routes | `ui/dashboard/dashboard.py` (5140 lines) | `do_GET` ~L2694, `do_POST` ~L3390, then PUT/DELETE; `_dispatch` error handling ~L2181 |
| Whole SPA | `ui/dashboard/index.html` (8040 lines) | Sections `#tab-<name>`, big inline `<script>`s, vanilla JS, Tailwind via local `static/tailwind.js` (runtime, so any utility class works), `static/ui-kit.{css,js}` (has `ui.esc`, `ui.busy`, `mcConfirm`) |
| Office floor view (retro "Dunder Mifflin") | `ui/dashboard/static/office.js`, `office.css`, `portraitArt.js`, `maps/office.tmj`, `tilesets/*` | Data from `ui/dashboard/office_state.py` → `GET /api/office/state` |
| Router | `brain/router/smart_router.py`, `models/policies/model_policy.py`, `config/providers.json` | Multi-factor scoring |
| Tasks/jobs | `tasks/manager.py`, `brain/orchestrator/*` | Reads swarm tasks read-only from `~/agentic-brain/swarm/tasks/{pending,in-progress,completed,escalated}` |
| Shared-brain tooling | `scripts/brain/*` | **Synced copy** from the other repo `/home/setoo/YashDevops/Agentic_os`. Avoid diverging it; only fix it if it breaks this repo |

Tabs (hash deep links): `#dashboard #office #events #flow #tasks #mc-jobs #worktrees #memory #agents #mc-accounts #mc-providers #mc-models #routing #tokens #mc-usage`.

Run the dashboard: `python3 ui/dashboard/dashboard.py` → http://127.0.0.1:3333. It is currently running as PID 13246, started from `.claude/launch.json`, which this session created.

Tests (stdlib unittest, hermetic by design via `tests/support/hermetic.py`):

```bash
python3 -m unittest discover -s tests/unit -t .
```

```bash
python3 -m unittest discover -s tests/integration -t .
```

To run a single module: `python3 -m unittest tests.unit.test_smart_router`.

---

## 3. Current git state (important)

```
 M brain/router/smart_router.py      <- user's WIP
 M config/providers.json             <- user's WIP
 M models/policies/model_policy.py   <- user's WIP
 M ui/dashboard/static/office.js     <- user's WIP
?? .claude/launch.json               <- created by this session (preview server config; harmless)
?? config/quotas.json                <- LEAKED by an audit sandbox, see §6.1. Needs the user's decision
?? HANDOVER.md                       <- this file
```

What the user's WIP does (`git diff`, 360+/139-):

- **smart_router.py:** new keyword rules for `antigravity-account-2077` (deep reasoning/security) and `-2078` (governance/compliance); broader cline/frontend keywords; prefix matching (`cline-account-N` matches `cline` rules); load penalty from active tasks; a new **fair-share** factor (`_recent_routes`); a decision cache `_last_route_decisions` used by `explain_routing`; `tool_support` scoring change.
- **model_policy.py:** catalogs for 2077/2078, plus prefix fallbacks in `select_model`.
- **providers.json:** adds `editor-refactoring` capability to four Antigravity accounts.
- **office.js:** maps each pixel character to a real account (`agentId`); Michael = auto-router ("GOD"). Adds real dispatch to `POST /api/dispatch` with the bearer token, per-character task/status sync from `/api/office/state`, and new copy.

Treat the WIP as code to review and improve, **not** to revert.

---

## 4. Safety rules (follow these; the previous session did)

- **Live dashboard :3333 holds the user's real state.** Use only GETs and the non-mutating POSTs (`/api/route`, `/api/routing/test`, `/api/memory/search`, `/api/context/preview`). Never dispatch/execute/continue/cancel/apply/reject/enable/disable/save against it. Token: `GET /api/token` → `.token`.
- **Never kill/signal processes you didn't start:** `antigravity-ide`, `kiro`, `language_server`, `basic-memory` (a heavy `basic-memory reindex` from another session was loading the CPU), the live dashboard (PID in `runtime/dashboard.pid`).
- Don't touch `~/.gemini` or `/home/setoo/YashDevops/Agentic_os`. `~/agentic-brain` is read-only unless the user agrees.
- Machine: **4 cores, heavily loaded** (load avg ~11). Workflow concurrency is only ~2 agents per workflow; running 2–3 workflows in parallel worked. Keep test runs targeted.
- Mutating tests go against a **hermetic sandbox dashboard** (§5.1). **But see §6.1:** the sandbox is *not* fully hermetic.

---

## 5. Tooling built this session (recreate if `/tmp` was cleared)

Scratch dir used: `/tmp/claude-1000/-home-setoo-YashDevops-Agentic-shared-memory/536d77e5-b8f9-4f6c-9f03-b63f7999475d/scratchpad/` (tools in `tools/`, agent notes/screenshots in `audit/<label>/` and `ux/<label>/`). The session's `/tmp` may be gone, so here is the full source.

### 5.1 `hermetic_dashboard.py` — sandbox Mission Control on another port

This script imports `tests` first. That pins the fixture provider config, a random bearer token, temp state dirs, and refuses real agent CLI runs. It also moves the PID file so the sandbox doesn't collide with the live instance's single-instance guard.

```python
"""Usage: python3 hermetic_dashboard.py [port]   (first log line prints the bearer token)"""
import os, sys
REPO = "/home/setoo/YashDevops/Agentic_shared_memory"
sys.path.insert(0, REPO)
os.chdir(REPO)
import tests  # noqa: F401  installs hermetic defaults
from ui.dashboard import dashboard
from pathlib import Path
dashboard.PID_FILE = Path(os.environ["BRAIN_TESTS_RUN_ROOT"]) / "dashboard.pid"
port = int(sys.argv[1]) if len(sys.argv) > 1 else 3399
print("HERMETIC token:", os.environ.get("MISSION_CONTROL_AUTH_TOKEN"), "BRAIN_DIR:", os.environ.get("BRAIN_DIR"), flush=True)
dashboard.run_server(port)
```

Run it in the background with a timeout: `timeout 1500 python3 hermetic_dashboard.py 3411 > herm.log 2>&1 &`. Wait about 12s before using it. Its task state starts empty.

### 5.2 `shot.js` — headless screenshot + console/network error capture

Plain `google-chrome --headless --screenshot` captured the page before the SPA loaded, so it was unreliable here. This script uses playwright-core from another local project plus the cached Playwright headless shell.

```js
// node shot.js <url> <out.png|-> [--w 1440] [--h 900] [--wait 6000] [--full] [--click css]... [--eval js]...
const { chromium } = require('/home/setoo/YashDevops/Khara AI/node_modules/playwright-core');
const path = require('path');
const exe = path.join(process.env.HOME, '.cache/ms-playwright/chromium_headless_shell-1194/chrome-linux/headless_shell');
(async () => {
  const a = process.argv.slice(2); const url = a[0], out = a[1];
  const opt = { w: 1440, h: 900, wait: 6000, full: false, evals: [], clicks: [] };
  for (let i = 2; i < a.length; i++) {
    if (a[i] === '--w') opt.w = +a[++i]; else if (a[i] === '--h') opt.h = +a[++i];
    else if (a[i] === '--wait') opt.wait = +a[++i]; else if (a[i] === '--full') opt.full = true;
    else if (a[i] === '--eval') opt.evals.push(a[++i]); else if (a[i] === '--click') opt.clicks.push(a[++i]);
  }
  const browser = await chromium.launch({ executablePath: exe, args: ['--no-sandbox'] });
  const page = await browser.newPage({ viewport: { width: opt.w, height: opt.h } });
  const log = { console: [], pageErrors: [], badRequests: [], evals: [] };
  page.on('console', m => { if (['error', 'warning'].includes(m.type())) log.console.push(`[${m.type()}] ${m.text()}`.slice(0, 400)); });
  page.on('pageerror', e => log.pageErrors.push(String(e && e.stack || e).slice(0, 600)));
  page.on('requestfailed', r => { if (!r.url().includes('/api/events/stream')) log.badRequests.push(`FAILED ${r.method()} ${r.url()} ${r.failure()?.errorText}`); });
  page.on('response', r => { if (r.status() >= 400) log.badRequests.push(`${r.status()} ${r.request().method()} ${r.url()}`); });
  await page.goto(url, { waitUntil: 'domcontentloaded', timeout: 30000 });
  await page.waitForTimeout(opt.wait);
  for (const sel of opt.clicks) { try { await page.click(sel, { timeout: 5000 }); await page.waitForTimeout(1500); } catch (e) { log.evals.push(`click ${sel} failed: ${e.message.slice(0, 200)}`); } }
  for (const ex of opt.evals) { try { log.evals.push(await page.evaluate(ex)); } catch (e) { log.evals.push(`eval error: ${e.message.slice(0, 300)}`); } }
  if (out && out !== '-') await page.screenshot({ path: out, fullPage: opt.full });
  console.log(JSON.stringify(log, null, 1));
  await browser.close();
})().catch(e => { console.error('shot.js failed:', e); process.exit(1); });
```

The Overview needs about 8s (`--wait 8000`) to finish loading.

---

## 6. Findings so far

**Legend:** ✅ = observed or reproduced by the main session. 🔎 = reported by one auditor with code evidence, **not yet independently verified** (the verify stage was stopped). Verify each 🔎 item against the code before fixing it.

### 6.1 Must handle first

1. ✅ **`config/quotas.json` was written into the real repo by an audit sandbox.** `brain/analytics/quota_manager.py:106` hard-codes `<repo>/config/quotas.json` and ignores the hermetic temp root. It contains junk test quotas that would **throttle real work on the next live dashboard restart**: `kiro` $5/day, `cline-account-1` 1000 tokens/month, and a bogus `auditprov` with `"abc"`. The live instance is unaffected for now (`GET /api/quotas` on :3333 → empty); it only loads the file at startup. The file did not exist at session start. Moving it aside was blocked by the permission classifier, so **ask the user whether to delete it**. Then fix `QuotaManager` to honour an env override / the hermetic run root, and add it to the hermetic setup and `test_zz_repo_state_unchanged`.
2. ✅ **Stray test task pollutes the live Overview.** `~/agentic-brain/swarm/tasks/{completed,escalated}/task-stub.json` (Sep 22) was written by `scripts/brain/test_capacity_failover.py`. 🔎 It always shows as **"Latest task … Created just now"** because `tasks/manager.py:~301` and `~523` stamp `created_at = now()` on every read for swarm records with no `created_at`. Fix: fall back to `started_at` → `completed_at` → file mtime. When an id appears in several folders, keep the newest record. Ask the user before deleting the stub files from `~/agentic-brain`. Make that test script hermetic.

### 6.2 Broken functions (UI)

3. 🔎 **"Fix stuck tasks" button does nothing.** `triggerReconcile()` (`index.html` ~L7675) POSTs `/api/task/reconcile`, but the server only serves `/api/tasks/reconcile` (`dashboard.py` ~L3552). The result is a silent 404. Fix the URL, pass the button to `ui.busy`, and show a result toast. The auditor verified this in a sandbox.
4. 🔎 **"Resume work" starts an agent run with no confirmation**, even when there's no handoff. It also reports "Swarm resumed" for any HTTP 200, including `{status:'terminal'}` (`index.html` ~L7668). Fix: add an `mcConfirm()` that shows the next step and agent, and handle the "nothing to resume" case.
5. ✅ **Office floor renders an empty grey-green box**: no tiles or characters, and the terminal pane is empty. The bottom agent strip overflows, so the Stanley card is cut off. Root cause is not yet investigated. Suspects: map/tileset load, canvas sized while the tab is hidden, rAF loop. The office auditor never ran.
6. 🔎 **Overview "Send to" dropdown is hard-coded** (`index.html` ~L707-713). It lists stale ids and omits 7 of 10 live accounts. Build it from `/api/accounts` the way `populateAgentFilter` (~L4373) does, escaping with `ui.esc`.
7. 🔎 **"Providers up 6 of 4"** in System details. `dashboard.py` ~L2596-2605 double-counts ids across `providers` and `ai_providers`. Fix: count a set of online ids, intersected with all ids. Related: API providers whose credential is stored on the account can never count as online (~L2601).
8. 🔎 "Tasks failed" tile shows 56 (excludes REJECTED) but the Tasks board "Failed / stopped" column shows 61.
9. 🔎 **Swarm-task SSE watcher always throws** (`dashboard.py` ~L389): it publishes a string event name where an `EventType` is expected, and the bare `except: pass` hides the error, so no live swarm events are ever emitted. Fix: map the strings to `EventType`, but keep `_SWARM_STATE_EVENTS` string values because tests assert them.
10. 🔎 An unhandled exception in a GET handler drops the socket with no HTTP response (`_dispatch` ~L2181), so the UI shows the whole API as offline. Fix: log it and return a generic 500 JSON if headers haven't been sent (SSE excluded).
11. 🔎 A stale or rotated token is only detected via the public `/api/status` (`index.html` ~L4209), so the board freezes silently. Check 401 on all refresh calls, then re-init auth once.

### 6.3 Performance (UI polls every 3s on every tab: about 17.6 MB per 30s while idle)

Measured on the live instance: `/api/resources` 8.4s · `/api/router/history` 2.5s / 464 KB · `/api/tasks` **1.7 MB** · `/api/status` ~1s · `/api/providers` ~1s.

12. 🔎 `/api/status` misses its cache on every poll, and each miss parses the whole **10.5 MB routing log** for 5 records that nothing reads (`dashboard.py` ~L2589). Fix: tail-read `get_routing_history` in 64 KB blocks, and reuse `orchestrator.router` instead of constructing `SmartRouter(registry)`.
13. 🔎 `/api/tasks`: 79% of the payload is `result`/`output`. Add opt-in `?view=summary` (keep the default shape). The UI should use it for polling; `openTaskModal` should fetch `/api/task?task_id=` for full output. Also cache the serialized body keyed on file mtimes.
14. 🔎 `/api/providers` makes live authenticated HTTP health calls per provider on every request (~L3039). Serve it from a TTL cache keyed by provider+account; the explicit `POST …/health` stays live and writes to the cache.
15. 🔎 `/api/resources` rebuilds every registry and runs ~20 subprocesses per call (~L3377). Memoize with a lock and ~10 min TTL, invalidate on config/repo mtime, and run CLI probes concurrently.
16. 🔎 Unbounded limits: `/api/routing/history?limit=-1` returns the whole 10.5 MB log, and `routing/inspect` matches job ids by substring. Clamp to 1..500 and match exactly.
17. 🔎 The Office canvas keeps a 60 fps loop plus 4s `/api/office/state` polling while the tab is hidden. Pause both when hidden. Also only poll `/api/tasks` on tabs that need it.

### 6.4 Router WIP review (user's uncommitted code). Discuss with the user before changing behaviour.

18. 🔎 **Ties / over-broad keywords:** the new 2077/2078 regexes use generic single words (`policy`, `verification`, `governance`, `orchestration`) that duplicate accounts 1 and 3. With uniform `tool_support`, scores tie exactly and registration order decides. Example: "write pytest tests for the password policy module" goes to a governance account instead of `kiro-cli`. Fix: use specific phrases and add a deterministic merit-based tie-break.
19. 🔎 **Dry-runs mutate fair-share:** `route()` always increments `_recent_routes` and fills `_last_route_decisions`, including `/api/route` previews, `/api/routing/test`, and `explain_routing`. Meanwhile `/api/jobs` auto-routing and the preview disagree with real dispatch. Fix: `route(..., record: bool = True)` (or `commit(decision)`), recording only in `Orchestrator.plan_and_dispatch` and the `/api/jobs` submit path.
20. 🔎 **Perf:** the load-penalty block calls `task_manager.list_tasks()` once per candidate on every route, on top of the existing per-agent call (~L552). Compute active counts once per scoring pass, over `TaskStatus.READY/PLANNING/RUNNING` (verify the enum values: the WIP compares against upper-case strings such as `"IN_PROGRESS"`, which may not exist).
21. 🔎 `explain_routing` returns a decision cached by task text alone, ignoring kwargs (preferred agent/model/mode) and current health (~L1114). Use the cache only when there are no kwargs, and re-check health.
22. 🔎 2077/2078 are mapped to account-1's model catalog, so `select_model` can pick models outside those accounts' configured `models` lists. Add an `allowed_models` filter.
23. 🔎 Adding `editor-refactoring` to accounts 1/3/2077/2078 moves rename/extract tasks away from the refactoring specialist (account-2).
24. 🔎 The routing-inspect modal (`index.html` ~L7915 `FACTORS`) doesn't show the new `fair_share_balance` factor. Add it.
25. 🔎 The WIP is untested: `tests/fixtures/providers.json` lacks 2077/2078 and the new capabilities, so the suite stays green while live routing regresses. Add tests for items 18–21.

### 6.5 Security (low severity, local-only app)

26. 🔎 `task_id` is not validated (`tasks/manager.py` ~L262): `GET /api/task` can read limited fields from JSON outside the store. Validate with `^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$` and return 400.
27. 🔎 The Google OAuth callback uses the wizard id as `state` instead of the single-use nonce the Cline flow uses (`dashboard.py` ~L4387). Also set the `postMessage` target origin.
28. ⚠️ Not yet audited: XSS through `innerHTML` interpolation of task titles and agent names. The WIP `office.js` interpolates `t.title`, `agentName`, and `a.id` into `innerHTML` in the terminal/tasks/workers renders. Check every `innerHTML` in `office.js` and `index.html` and escape with `ui.esc`.

### 6.6 UX proposals (from the overview/shell reviewer; the other UX reviewers were stopped)

- **P2:** Add a "Needs attention" strip on Overview (waiting tasks → Run now; failed → open the triage view).
- **P2:** KPI tiles open the matching Tasks column (`opsOpenTasks(bucket)` → `showTab('tasks')` + scroll + highlight), with counts that agree with the Tasks tab.
- **P2:** The sidebar hides its last items (Models, Routing, Tokens, Quotas) at 1366×768/1024×768 and even at 1440×900, with no scroll cue. Make it denser under `@media (max-height: 900px)`, add a scroll fade, and show fleet status when collapsed.
- **P2:** Replace the "Worker adapters" jargon tile with "Providers up x of y" (click → Providers).
- **P3:** Keyed toasts that replace progress messages. Humanize network errors ("Can't reach the dashboard server. Your text is kept").
- **P3:** Shortcuts dialog lists every `g <letter>`, traps focus, and restores focus on Esc; add `n` = new task.
- **P3:** Tablet/phone header cleanup; put the Overview task form above the fold on phones and clamp long handoff text.

---

## 7. Audit coverage: what's done and what isn't

| Dimension | Status |
|---|---|
| Unit test baseline | ✅ **951 tests OK** (4 skipped), ~224s |
| Router WIP review | 🔎 done (§6.4), not verified |
| Dashboard GET handlers / perf | 🔎 done (§6.2–6.3), not verified |
| Overview/shell UX | 🔎 done (§6.6) |
| Dashboard mutating handlers (POST/PUT/DELETE, validation, wizard) | ❌ stopped mid-run |
| Orchestration (tasks/jobs/failover/worktrees/locks) | ❌ stopped mid-run |
| Frontend: shell & ops tabs JS | ❌ stopped mid-run |
| Frontend: fleet/config tabs JS | ❌ stopped mid-run |
| UX: work tabs, fleet tabs | ❌ stopped mid-run |
| Providers/accounts/adapters, memory/context/analytics, integration+e2e tests & test pollution, full live API contract sweep, Office floor deep-dive, live click-through of every tab (1440/1024/390 px), UX Office + accessibility, UX synthesis | ❌ never started |

Raw auditor output (JSON, including full evidence and suggested fixes) is in the workflow journals, if they still exist:
`~/.claude/projects/-home-setoo-YashDevops-Agentic-shared-memory/536d77e5-b8f9-4f6c-9f03-b63f7999475d/subagents/workflows/wf_*/journal.jsonl` (each line with `"type":"result"` holds one agent's output).

---

## 8. Recommended plan for the next model

1. **Ask the user** about: (a) deleting `config/quotas.json` (§6.1-1); (b) deleting `~/agentic-brain/swarm/tasks/*/task-stub.json` (§6.1-2); (c) the router WIP direction (§6.4): keep 2077/2078 keyword routing but with specific phrases plus a tie-break? keep the fair-share factor?
2. **Finish the audit** for the ❌ rows in §7 (parallel subagents; read-only on :3333; hermetic sandboxes for mutations). First fix the sandbox leak (§6.1-1) or point agents at a sandbox whose quota file is redirected. Then **adversarially verify** every 🔎 finding before fixing it.
3. **Fix phase.** Partition by file so parallel agents don't collide. `ui/dashboard/index.html` is a single 8k-line file, so serialize edits to it, or give each agent a distinct tab region. Suggested order:
   1. data/state bugs (§6.1, 6.2 items 7–11)
   2. broken buttons (3, 4, 6)
   3. Office floor rendering (5)
   4. performance (§6.3, keep API shapes backward compatible; the UI and tests consume them)
   5. router WIP fixes plus tests (§6.4)
   6. XSS escaping (28)
   7. UX items (§6.6 plus the rest of the UX review)
4. **Every fix gets a regression test** where feasible (unittest, hermetic). After each batch: run targeted modules, then the full unit + integration suites, and screenshot every tab at 1440×900, 1024×768 and 390×844 with `shot.js` to check for zero console/page errors and no layout overflow.
5. Keep the stack: vanilla JS + Tailwind utilities + ui-kit; **no new frameworks, CDNs, or Python dependencies**. Match the surrounding code style.
6. Don't commit unless the user asks. If they do, branch off `Dev/fix` and follow the repo's commit style (`feat(scope): …` / `fix(scope): …`).
