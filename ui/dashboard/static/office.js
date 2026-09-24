// office.js — Dunder Mifflin Retro Office & Command Center for Mission Control
// Exact retro pixel-art aesthetic matching munder-difflin with Dunder Mifflin window chrome,
// Tiled office floor map, procedural character sprites, speech bubbles, Command Center tabs,
// live terminal console, and bottom agent roster strip.

(function () {
  'use strict';

  // ── Global Canvas & Map Config ─────────────────────────────────────────────
  const MAP_W = 544; // 34 tiles * 16px
  const MAP_H = 352; // 22 tiles * 16px
  const TILE_SIZE = 16;
  const MAP_COLS = 34;
  const MAP_ROWS = 22;

  const TILESET_DEFS = [
    { name: 'office', firstgid: 1, cols: 16, url: '/static/tilesets/office-tileset.png' },
    { name: 'a5', firstgid: 513, cols: 16, url: '/static/tilesets/a5-office-floors-walls.png' },
    { name: 'interiors', firstgid: 1025, cols: 16, url: '/static/tilesets/interiors.png' }
  ];

  // Lit PC monitor overlay tiles (office tileset gids 367, 368, 383, 384)
  const LIT_MONITOR_TILES = [
    { gid: 367, dx: 0, dy: 0 },
    { gid: 368, dx: 1, dy: 0 },
    { gid: 383, dx: 0, dy: 1 },
    { gid: 384, dx: 1, dy: 1 }
  ];

  // Desk monitor positions where PCs are located on the floor map
  const DESK_MONITOR_POSITIONS = [
    { x: 48, y: 48 },   // Michael's desk
    { x: 32, y: 192 },  // Row 1 Desk 1 (Jim)
    { x: 96, y: 192 },  // Row 1 Desk 2 (Dwight)
    { x: 160, y: 192 }, // Row 1 Desk 3 (Ryan)
    { x: 224, y: 192 }, // Row 1 Desk 4 (Meredith)
    { x: 288, y: 192 }, // Row 1 Desk 5
    { x: 352, y: 192 }, // Row 1 Desk 6
    { x: 32, y: 272 },  // Row 2 Desk 1 (Pam)
    { x: 96, y: 272 },  // Row 2 Desk 2 (Kevin)
    { x: 160, y: 272 }, // Row 2 Desk 3 (Stanley)
    { x: 224, y: 272 }, // Row 2 Desk 4 (Angela)
    { x: 288, y: 272 }, // Row 2 Desk 5 (Oscar)
    { x: 352, y: 272 }, // Row 2 Desk 6
    { x: 320, y: 96 },  // Annex Desk 1 (Toby)
    { x: 368, y: 96 }   // Annex Desk 2
  ];

  // ── Office Cast Specification ──────────────────────────────────────────────
  const INITIAL_CAST = [
    {
      id: 'michael',
      name: 'Michael',
      label: 'MICHAEL',
      charName: 'michael',
      isGod: true,
      status: 'idle',
      action: 'idle',
      note: 'hive',
      role: 'GOD',
      badge: 'GOD',
      x: 48,
      y: 56,
      dir: 'down',
      sitting: true,
      progress: 0,
      bubble: null
    },
    {
      id: 'jim',
      name: 'Jim',
      label: 'JIM',
      charName: 'jim',
      isGod: false,
      status: 'working',
      action: 'awaiting',
      note: 'awaiting',
      role: 'Sales',
      badge: 'working',
      x: 32,
      y: 204,
      dir: 'up',
      sitting: true,
      progress: 68,
      bubble: 'awaiting'
    },
    {
      id: 'pam',
      name: 'Pam',
      label: 'PAM',
      charName: 'pam',
      isGod: false,
      status: 'working',
      action: 'awaiting',
      note: 'awaiting',
      role: 'Reception / Artist',
      badge: 'working',
      x: 184,
      y: 242,
      dir: 'down',
      sitting: false,
      progress: 52,
      bubble: 'awaiting'
    },
    {
      id: 'kevin',
      name: 'Kevin',
      label: 'KEVIN',
      charName: 'kevin',
      isGod: false,
      status: 'working',
      action: 'starting up',
      note: 'starting up',
      role: 'Accounting',
      badge: 'working',
      x: 250,
      y: 256,
      dir: 'right',
      sitting: false,
      progress: 35,
      bubble: 'awaiting'
    },
    {
      id: 'ryan',
      name: 'Ryan',
      label: 'RYAN',
      charName: 'ryan',
      isGod: false,
      status: 'working',
      action: 'starting up',
      note: 'starting up',
      role: 'The Temp',
      badge: 'working',
      x: 160,
      y: 204,
      dir: 'up',
      sitting: true,
      progress: 20,
      bubble: 'starting up'
    },
    {
      id: 'stanley',
      name: 'Stanley',
      label: 'STANL..',
      charName: 'stanley',
      isGod: false,
      status: 'working',
      action: 'starting up',
      note: 'starting up',
      role: 'Sales / Crosswords',
      badge: 'working',
      x: 264,
      y: 256,
      dir: 'left',
      sitting: false,
      progress: 40,
      bubble: 'starting up'
    },
    {
      id: 'meredith',
      name: 'Meredith',
      label: 'MEREDITH',
      charName: 'meredith',
      isGod: false,
      status: 'working',
      action: 'InstaContent',
      note: 'InstaContent',
      role: 'Supplier Relations',
      badge: 'working',
      x: 224,
      y: 204,
      dir: 'up',
      sitting: true,
      progress: 75,
      bubble: 'awaiting'
    },
    {
      id: 'dwight',
      name: 'Dwight',
      label: 'DWIGHT',
      charName: 'dwight',
      isGod: false,
      status: 'working',
      action: 'starting up',
      note: 'starting up',
      role: 'Assistant (to the) RM',
      badge: 'working',
      x: 96,
      y: 204,
      dir: 'up',
      sitting: true,
      progress: 30,
      bubble: 'starting up',
      inRoster: false
    },
    {
      id: 'toby',
      name: 'Toby',
      label: 'TOBY',
      charName: 'toby',
      isGod: false,
      status: 'idle',
      action: 'HR review',
      note: 'HR',
      role: 'Human Resources',
      badge: 'idle',
      x: 320,
      y: 110,
      dir: 'up',
      sitting: true,
      progress: 10,
      bubble: null,
      inRoster: false
    }
  ];

  // ── Module State ───────────────────────────────────────────────────────────
  let canvas = null;
  let ctx = null;
  let bgCanvas = null;
  let fgCanvas = null;
  let tmjData = null;
  const tilesetImages = {};
  const charFrameCanvases = {};
  let animationFrameId = null;
  let lastTime = performance.now();
  let selectedAgentId = 'michael';
  let activeTab = 'terminal';
  let officeState = null;
  let pollInterval = null;
  let currentZoom = 12;

  const characters = JSON.parse(JSON.stringify(INITIAL_CAST));

  // ── Asset Loading Helpers ──────────────────────────────────────────────────
  function loadImage(src) {
    return new Promise((resolve, reject) => {
      const img = new Image();
      img.onload = () => resolve(img);
      img.onerror = (e) => reject(new Error('Failed to load image: ' + src));
      img.src = src;
    });
  }

  function loadJson(url) {
    return fetch(url).then((res) => {
      if (!res.ok) throw new Error('HTTP ' + res.status + ' fetching ' + url);
      return res.json();
    });
  }

  function frameBufToCanvas(buf, w, h) {
    const c = document.createElement('canvas');
    c.width = w;
    c.height = h;
    const sctx = c.getContext('2d');
    const imgData = sctx.createImageData(w, h);
    imgData.data.set(buf);
    sctx.putImageData(imgData, 0, 0);
    return c;
  }

  // ── Map Pre-rendering ──────────────────────────────────────────────────────
  function drawGidTile(targetCtx, gid, dx, dy) {
    if (!gid || gid <= 0) return;
    let def = null;
    if (gid >= 1025) def = TILESET_DEFS[2]; // interiors
    else if (gid >= 513) def = TILESET_DEFS[1]; // a5
    else def = TILESET_DEFS[0]; // office

    const img = tilesetImages[def.name];
    if (!img) return;

    const idx = gid - def.firstgid;
    const col = idx % def.cols;
    const row = Math.floor(idx / def.cols);
    const sx = col * TILE_SIZE;
    const sy = row * TILE_SIZE;

    targetCtx.drawImage(img, sx, sy, TILE_SIZE, TILE_SIZE, dx, dy, TILE_SIZE, TILE_SIZE);
  }

  function preRenderMap() {
    if (!tmjData) return;

    bgCanvas = document.createElement('canvas');
    bgCanvas.width = MAP_W;
    bgCanvas.height = MAP_H;
    const bgCtx = bgCanvas.getContext('2d');
    bgCtx.imageSmoothingEnabled = false;

    fgCanvas = document.createElement('canvas');
    fgCanvas.width = MAP_W;
    fgCanvas.height = MAP_H;
    const fgCtx = fgCanvas.getContext('2d');
    fgCtx.imageSmoothingEnabled = false;

    // Render floor, walls, and furniture-below into bgCanvas
    const bgLayerNames = ['floor', 'walls', 'furniture-below'];
    for (const name of bgLayerNames) {
      const layer = tmjData.layers.find((l) => l.name === name);
      if (layer && layer.data) {
        for (let r = 0; r < MAP_ROWS; r++) {
          for (let c = 0; c < MAP_COLS; c++) {
            const gid = layer.data[r * MAP_COLS + c];
            if (gid > 0) {
              drawGidTile(bgCtx, gid, c * TILE_SIZE, r * TILE_SIZE);
            }
          }
        }
      }
    }

    // Render furniture-above into fgCanvas
    const fgLayer = tmjData.layers.find((l) => l.name === 'furniture-above');
    if (fgLayer && fgLayer.data) {
      for (let r = 0; r < MAP_ROWS; r++) {
        for (let c = 0; c < MAP_COLS; c++) {
          const gid = fgLayer.data[r * MAP_COLS + c];
          if (gid > 0) {
            drawGidTile(fgCtx, gid, c * TILE_SIZE, r * TILE_SIZE);
          }
        }
      }
    }
  }

  // ── Character Sprite Cache ─────────────────────────────────────────────────
  function initCharacterSprites() {
    if (!window.PortraitArt || typeof window.PortraitArt.sceneFrameBufs !== 'function') {
      console.warn('PortraitArt.sceneFrameBufs is unavailable; using procedural fallback');
      return;
    }
    const { SCENE_W, SCENE_H, sceneFrameBufs } = window.PortraitArt;
    const castNames = ['michael', 'jim', 'pam', 'dwight', 'kevin', 'ryan', 'stanley', 'meredith', 'toby'];

    for (const name of castNames) {
      const bufs = sceneFrameBufs(name);
      charFrameCanvases[name] = {
        front: bufs.front.map((buf) => frameBufToCanvas(buf, SCENE_W, SCENE_H)),
        back: bufs.back.map((buf) => frameBufToCanvas(buf, SCENE_W, SCENE_H))
      };
    }
  }

  // ── Drawing Animated Desk Screens ──────────────────────────────────────────
  function drawDeskScreens(targetCtx, timeS) {
    const img = tilesetImages['office'];
    if (!img) return;

    for (const pos of DESK_MONITOR_POSITIONS) {
      // Draw lit monitor 2x2 tiles
      for (const t of LIT_MONITOR_TILES) {
        const idx = t.gid - 1;
        const col = idx % 16;
        const row = Math.floor(idx / 16);
        const sx = col * TILE_SIZE;
        const sy = row * TILE_SIZE;
        targetCtx.drawImage(
          img,
          sx,
          sy,
          TILE_SIZE,
          TILE_SIZE,
          pos.x + t.dx * TILE_SIZE,
          pos.y + t.dy * TILE_SIZE,
          TILE_SIZE,
          TILE_SIZE
        );
      }

      // Animated scrolling code lines inside monitor screen
      const screenX = pos.x + 3;
      const screenY = pos.y + 5;
      const screenW = 25;
      const screenH = 12;

      targetCtx.fillStyle = 'rgba(207, 230, 255, 0.65)';
      for (let i = 0; i < 2; i++) {
        const phase = (timeS * 3.2 + i * (screenH / 2)) % screenH;
        const lineY = Math.round(screenY + screenH - 1 - phase);
        const lineW = 6 + ((Math.floor(timeS * 2) + i * 5) % 8);
        targetCtx.fillRect(screenX + 2, lineY, lineW, 1);
      }

      // Blinking terminal cursor
      if (Math.floor(timeS * 2) % 2 === 0) {
        targetCtx.fillStyle = '#FFFFFF';
        targetCtx.fillRect(screenX + 2, screenY + screenH - 3, 2, 2);
      }
    }
  }

  // ── Render Loop ────────────────────────────────────────────────────────────
  function render(timeMs) {
    animationFrameId = requestAnimationFrame(render);
    const dt = (timeMs - lastTime) / 1000;
    lastTime = timeMs;
    const timeS = timeMs / 1000;

    if (!ctx || !bgCanvas || !fgCanvas) return;

    // 1. Draw static background (floor, walls, furniture-below)
    ctx.drawImage(bgCanvas, 0, 0);

    // 2. Draw lit desk screens with animated terminal pulses
    drawDeskScreens(ctx, timeS);

    // 3. Draw characters sorted by Y for correct isometric depth
    const sortedChars = characters.slice().sort((a, b) => a.y - b.y);

    for (const char of sortedChars) {
      const frames = charFrameCanvases[char.charName];
      if (!frames) continue;

      const isBack = char.dir === 'up';
      const frameList = isBack ? frames.back : frames.front;

      // Animation frame selection
      let frameIndex = 0;
      if (!char.sitting) {
        // Gentle standing weight shift
        frameIndex = Math.floor(timeS * 1.5 + char.x) % frameList.length;
      }

      const frameCanvas = frameList[frameIndex] || frameList[0];

      // Subtle breathing / bobbing
      const bob = char.sitting ? 0 : Math.sin(timeS * 2.5 + char.x) * 0.6;
      const drawX = Math.round(char.x - frameCanvas.width / 2);
      const drawY = Math.round(char.y - frameCanvas.height + bob);

      // Selected character gold indicator halo
      if (char.id === selectedAgentId) {
        ctx.save();
        ctx.fillStyle = 'rgba(245, 158, 11, 0.25)';
        ctx.beginPath();
        ctx.ellipse(char.x, char.y - 2, 9, 4, 0, 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = '#F59E0B';
        ctx.lineWidth = 1;
        ctx.stroke();
        ctx.restore();
      }

      ctx.drawImage(frameCanvas, drawX, drawY);
    }

    // 4. Draw foreground layer (furniture-above) to occlude characters behind desks
    ctx.drawImage(fgCanvas, 0, 0);

    // 5. Update floating speech bubbles
    updateSpeechBubbles(timeS);
  }

  // ── Speech Bubbles ─────────────────────────────────────────────────────────
  function updateSpeechBubbles(timeS) {
    const container = document.getElementById('dunder-bubbles');
    if (!container) return;

    // Check if bubbles already match our list of active bubbles matching reference image
    const bubbleList = [
      // Michael office wall indicator: clock icon + idle box + calendar icon
      {
        id: 'michael-wall-clock',
        x: 48,
        y: 28,
        isCustom: true,
        html: `
          <div style="display:inline-flex; align-items:center; gap:4px; font-size:10px; font-weight:700;">
            <i class="fa-regular fa-clock" style="color:#B91C1C; font-size:12px;"></i>
            <span style="background:#FFF; border:1px solid #2B2118; padding:1px 4px; border-radius:3px;">idle</span>
            <i class="fa-regular fa-calendar-days" style="color:#B91C1C; font-size:12px;"></i>
          </div>
        `
      },
      // Jim desk bubble: awaiting
      { id: 'jim-bubble', x: 32, y: 190, text: 'awaiting', status: 'awaiting' },
      // Ryan desk bubble: starting up with blue monitor icon
      {
        id: 'ryan-bubble',
        x: 160,
        y: 190,
        isCustom: true,
        html: `
          <div class="dunder-bubble starting" style="position:static; transform:none; display:inline-flex; align-items:center; gap:3px;">
            starting up <span class="bubble-icon"><i class="fa-solid fa-desktop"></i></span>
          </div>
        `
      },
      // Pam standing in aisle bubble: awaiting
      { id: 'pam-bubble', x: 184, y: 228, text: 'awaiting', status: 'awaiting' },
      // Breakroom doorway area (Kevin & Stanley): 3 stacked speech bubbles
      { id: 'door-bubble-1', x: 260, y: 216, text: 'awaiting', status: 'awaiting' },
      {
        id: 'door-bubble-2',
        x: 264,
        y: 232,
        isCustom: true,
        html: `
          <div class="dunder-bubble starting" style="position:static; transform:none; display:inline-flex; align-items:center; gap:3px;">
            starting up <span class="bubble-icon"><i class="fa-solid fa-desktop"></i></span>
          </div>
        `
      },
      { id: 'door-bubble-3', x: 260, y: 248, text: 'starting up', status: 'starting' },
      // Meredith desk bubble: awaiting
      { id: 'meredith-bubble', x: 224, y: 190, text: 'awaiting', status: 'awaiting' }
    ];

    if (!container.dataset.initialized) {
      container.innerHTML = '';
      for (const b of bubbleList) {
        const el = document.createElement('div');
        el.id = b.id;
        el.className = 'dunder-bubble ' + (b.status || '');
        if (b.isCustom) {
          el.innerHTML = b.html;
          el.style.background = 'transparent';
          el.style.border = 'none';
          el.style.boxShadow = 'none';
          el.style.padding = '0';
        } else {
          el.textContent = b.text;
        }
        el.style.left = (b.x / MAP_W) * 100 + '%';
        el.style.top = (b.y / MAP_H) * 100 + '%';
        container.appendChild(el);
      }
      container.dataset.initialized = 'true';
    }

    // Subtle gentle float
    const offset = Math.sin(timeS * 2) * 1.5;
    for (const b of bubbleList) {
      const el = document.getElementById(b.id);
      if (el && !b.isCustom) {
        el.style.transform = `translate(-50%, calc(-100% + ${offset}px))`;
      }
    }
  }

  // ── Bottom Agent Roster Strip ──────────────────────────────────────────────
  function renderRosterStrip() {
    const strip = document.getElementById('dunder-roster-strip');
    if (!strip) return;
    strip.innerHTML = '';

    const rosterList = characters.filter((c) => c.inRoster !== false);
    for (const char of rosterList) {
      const card = document.createElement('div');
      card.className = 'dunder-agent-card' + (char.id === selectedAgentId ? ' selected' : '');
      card.setAttribute('data-agent-id', char.id);
      card.onclick = () => selectAgent(char.id);

      // Card Header
      const head = document.createElement('div');
      head.className = 'agent-card-head';

      const nameSpan = document.createElement('span');
      nameSpan.className = 'agent-name';
      if (char.isGod) {
        nameSpan.innerHTML = `${char.label} <span style="background:#EFC662; color:#1A140E; font-size:9px; font-weight:900; padding:1px 3px; border-radius:2px; border:1px solid #2B2118; margin-left:2px;">GOD</span>`;
      } else {
        nameSpan.textContent = char.label;
      }

      const badgeSpan = document.createElement('span');
      badgeSpan.className = 'agent-badge ' + (char.isGod ? 'idle' : char.status);
      badgeSpan.innerHTML = `<span class="status-sq ${char.isGod ? 'idle' : char.status}"></span> ${char.isGod ? 'idle' : char.status}`;

      head.appendChild(nameSpan);
      head.appendChild(badgeSpan);
      card.appendChild(head);

      // Card Body
      const body = document.createElement('div');
      body.className = 'agent-card-body';

      // Portrait Canvas
      const portraitBox = document.createElement('div');
      portraitBox.className = 'agent-portrait-box';
      const pCanvas = document.createElement('canvas');
      pCanvas.width = 28;
      pCanvas.height = 44;
      if (window.PortraitArt && typeof window.PortraitArt.paintPortrait === 'function') {
        const pctx = pCanvas.getContext('2d');
        window.PortraitArt.paintPortrait(pctx, char.charName, 1.5);
      }
      portraitBox.appendChild(pCanvas);
      body.appendChild(portraitBox);

      // Meta Info
      const meta = document.createElement('div');
      meta.className = 'agent-card-meta';

      const note = document.createElement('div');
      note.className = 'agent-status-note';
      note.textContent = char.note || char.action;
      meta.appendChild(note);

      if (char.isGod) {
        const talkBtn = document.createElement('button');
        talkBtn.type = 'button';
        talkBtn.className = 'agent-talk-btn';
        talkBtn.innerHTML = '<i class="fa-solid fa-microphone"></i> talk';
        talkBtn.onclick = (e) => {
          e.stopPropagation();
          const input = document.getElementById('dunder-queue-input');
          if (input) input.focus();
        };
        meta.appendChild(talkBtn);
      } else {
        const progBar = document.createElement('div');
        progBar.className = 'agent-progress-bar';
        const progFill = document.createElement('div');
        progFill.className = 'agent-progress-fill';
        progFill.style.width = (char.progress || 30) + '%';
        progBar.appendChild(progFill);
        meta.appendChild(progBar);
      }

      body.appendChild(meta);
      card.appendChild(body);

      // Cyan corner decorative pip
      const pip = document.createElement('span');
      pip.className = 'corner-pip';
      card.appendChild(pip);

      strip.appendChild(card);
    }
  }

  // ── Agent Selection ────────────────────────────────────────────────────────
  function selectAgent(id) {
    selectedAgentId = id;
    const char = characters.find((c) => c.id === id);
    if (!char) return;

    // Update roster cards active class
    const cards = document.querySelectorAll('.dunder-agent-card');
    cards.forEach((c) => {
      if (c.getAttribute('data-agent-id') === id) c.classList.add('selected');
      else c.classList.remove('selected');
    });

    // Update Boss Card / Command Center Header
    const bossStatus = document.querySelector('.boss-status');
    if (bossStatus) {
      if (char.isGod) {
        bossStatus.innerHTML = `<span class="status-sq idle"></span> idle &nbsp; Michael runs the floor`;
      } else {
        bossStatus.innerHTML = `<span class="status-sq ${char.status}"></span> ${char.status} &nbsp; ${char.name} (${char.role})`;
      }
    }

    // Scroll card into view
    const activeCard = document.querySelector(`.dunder-agent-card[data-agent-id="${id}"]`);
    if (activeCard) {
      activeCard.scrollIntoView({ behavior: 'smooth', inline: 'nearest', block: 'nearest' });
    }
  }

  // ── Command Center Initial Terminal Content ────────────────────────────────
  const INITIAL_TERMINAL_HTML = `
    <p class="term-cmd-bar">&gt; Let's ask each of the agents what are they up to. In short,</p>
    <p class="term-bullet">● On it — sending each of the 7 agents a short &quot;what are you up to?&quot; query.</p>
    <p class="term-muted">Ran 1 shell command</p>
    <p class="term-bullet" style="margin-top: 6px;">● Sent a short &quot;what are you up to?&quot; query to all 7 agents — <strong>Jim, Pam, Kevin, Ryan, Stanley, Meredith, and Toby</strong>. The orchestrator will deliver them from my outbox; each is asked for a one- or two-line status (current work + next step, or idle/parked/blocked).</p>
    <p class="term-muted" style="margin-top: 4px;">Replies land in my inbox — I'll collect them and give you a consolidated one-line-per-agent rundown as they come in.</p>
    <p class="term-baked">* Baked for 18s</p>
    <p class="term-prompt">&gt; []</p>
  `;

  function initTerminalView() {
    const logsEl = document.getElementById('dunder-terminal-logs');
    if (logsEl && !logsEl.hasChildNodes()) {
      logsEl.innerHTML = INITIAL_TERMINAL_HTML;
    }
  }

  // ── Command Center Tabs Switching ──────────────────────────────────────────
  function setupTabs() {
    const tabs = document.querySelectorAll('.dunder-tab');
    tabs.forEach((btn) => {
      btn.onclick = () => {
        tabs.forEach((t) => t.classList.remove('active'));
        btn.classList.add('active');
        activeTab = btn.getAttribute('data-tab');
        renderTabContent(activeTab);
      };
    });
  }

  function renderTabContent(tab) {
    const logsEl = document.getElementById('dunder-terminal-logs');
    if (!logsEl) return;

    if (tab === 'terminal') {
      logsEl.innerHTML = INITIAL_TERMINAL_HTML;
      logsEl.scrollTop = logsEl.scrollHeight;
    } else if (tab === 'tasks') {
      if (!officeState || !officeState.tasks) {
        logsEl.innerHTML = `<p class="term-bullet">Loading tasks from Mission Control...</p>`;
        return;
      }
      let html = `<p class="term-cmd-bar">&gt; ACTIVE MISSION CONTROL TASKS (${officeState.tasks.length} total)</p>`;
      for (const t of officeState.tasks.slice(0, 15)) {
        html += `
          <div style="background:#FAF2E3; border:1px solid #D9CEB8; padding:6px 8px; margin:4px 0; border-radius:3px; color:#231C16;">
            <div style="display:flex; justify-content:space-between; font-weight:700;">
              <span style="color:#B45309;">#${t.id || 'task'}</span>
              <span style="color:#2F6F4E; text-transform:uppercase;">${t.stage || 'queued'}</span>
            </div>
            <div style="color:#231C16; margin-top:2px; font-weight:600;">${t.title || 'Untitled task'}</div>
            <div style="color:#7A6F62; font-size:10px; margin-top:2px;">Agent: ${t.assigned_to || 'unassigned'} · Stage: ${t.stage || 'pending'}</div>
          </div>
        `;
      }
      logsEl.innerHTML = html;
    } else if (tab === 'memory') {
      if (!officeState || !officeState.memory) {
        logsEl.innerHTML = `<p class="term-bullet">Loading shared brain memory notes...</p>`;
        return;
      }
      let html = `<p class="term-cmd-bar">&gt; SHARED BRAIN MEMORY NOTES (${officeState.memory.length} entries)</p>`;
      for (const m of officeState.memory.slice(0, 15)) {
        html += `
          <div style="background:#FAF2E3; border:1px solid #D9CEB8; padding:6px 8px; margin:4px 0; border-radius:3px; color:#231C16;">
            <div style="color:#1D4ED8; font-weight:700;">${m.title || m.id}</div>
            <div style="color:#4B3F35; font-size:11px; margin-top:2px;">${m.summary || m.snippet || ''}</div>
          </div>
        `;
      }
      logsEl.innerHTML = html;
    } else if (tab === 'workers') {
      if (!officeState || !officeState.agents) {
        logsEl.innerHTML = `<p class="term-bullet">Loading registered worker accounts...</p>`;
        return;
      }
      let html = `<p class="term-cmd-bar">&gt; REGISTERED AI WORKER ACCOUNTS (${officeState.agents.length} active)</p>`;
      for (const a of officeState.agents) {
        html += `
          <div style="display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid #E2D7C2; color:#231C16;">
            <span style="font-weight:700; color:#1F1914;">${a.label || a.id}</span>
            <span style="color:#6B5F54; font-size:11px;">${a.kind || 'agent'}</span>
            <span style="color:${a.status === 'offline' ? '#DC2626' : '#2F6F4E'}; font-weight:700;">[${a.status}]</span>
          </div>
        `;
      }
      logsEl.innerHTML = html;
    } else if (tab === 'activity') {
      if (!officeState || !officeState.flows) {
        logsEl.innerHTML = `<p class="term-bullet">No recent activity flows recorded.</p>`;
        return;
      }
      let html = `<p class="term-cmd-bar">&gt; RECENT ORCHESTRATION FLOWS (${officeState.flows.length})</p>`;
      for (const f of officeState.flows.slice(0, 15)) {
        html += `
          <div style="padding:5px 0; border-bottom:1px solid #E2D7C2; font-size:11px; color:#231C16;">
            <span style="color:#7C3AED; font-weight:700;">${f.type || 'flow'}</span>: 
            <span style="color:#1F1914;">${f.from_agent || 'orchestrator'} ➔ ${f.to_agent || 'worker'}</span>
            <span style="color:#7A6F62; float:right;">${f.time ? new Date(f.time).toLocaleTimeString() : ''}</span>
          </div>
        `;
      }
      logsEl.innerHTML = html;
    } else {
      logsEl.innerHTML = `
        <p class="term-cmd-bar">&gt; VIEW: ${tab.toUpperCase()}</p>
        <p class="term-muted">Realtime stream ready for ${tab}. All systems operational.</p>
      `;
    }
  }

  // ── Queue Message Dispatch ─────────────────────────────────────────────────
  function setupQueueComposer() {
    const input = document.getElementById('dunder-queue-input');
    const sendBtn = document.getElementById('dunder-send-btn');
    if (!input || !sendBtn) return;

    function handleSend() {
      const text = input.value.trim();
      if (!text) return;

      input.value = '';
      const logsEl = document.getElementById('dunder-terminal-logs');
      if (logsEl) {
        const p1 = document.createElement('p');
        p1.className = 'cmd';
        p1.style.marginTop = '8px';
        p1.textContent = `> ${text}`;
        logsEl.appendChild(p1);

        const p2 = document.createElement('p');
        p2.className = 'info';
        p2.style.marginTop = '4px';
        p2.innerHTML = `<span style="color:#22C55E;">●</span> Michael: Routing message to floor orchestrator...`;
        logsEl.appendChild(p2);

        logsEl.scrollTop = logsEl.scrollHeight;
      }

      // If Mission Control has an API token, post the directive
      fetch('/api/tasks', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: text, assigned_to: selectedAgentId })
      }).catch((e) => console.log('Task dispatch attempt:', e));
    }

    sendBtn.onclick = handleSend;
    input.onkeydown = (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        handleSend();
      }
    };
  }

  // ── Font Zoom Handlers ─────────────────────────────────────────────────────
  function setupZoomControls() {
    const zoomIn = document.getElementById('zoom-in');
    const zoomOut = document.getElementById('zoom-out');
    const zoomLabel = document.getElementById('zoom-level');
    const logsEl = document.getElementById('dunder-terminal-logs');

    if (zoomIn && zoomOut && logsEl) {
      zoomIn.onclick = () => {
        currentZoom = Math.min(18, currentZoom + 1);
        logsEl.style.fontSize = currentZoom + 'px';
        if (zoomLabel) zoomLabel.textContent = currentZoom + 'px';
      };
      zoomOut.onclick = () => {
        currentZoom = Math.max(9, currentZoom - 1);
        logsEl.style.fontSize = currentZoom + 'px';
        if (zoomLabel) zoomLabel.textContent = currentZoom + 'px';
      };
    }
  }

  // ── Fullscreen & Window Actions ────────────────────────────────────────────
  function setupWindowActions() {
    const fsBtn = document.getElementById('dunder-fs-btn');
    const win = document.querySelector('.dunder-window');
    if (fsBtn && win) {
      fsBtn.onclick = () => {
        if (!document.fullscreenElement) {
          win.requestFullscreen().catch(() => {});
        } else {
          document.exitFullscreen().catch(() => {});
        }
      };
    }
  }

  // ── Live State Fetching & Synchronization ──────────────────────────────────
  async function fetchOfficeState() {
    try {
      let res;
      if (typeof window.fetchWithAuth === 'function') {
        res = await window.fetchWithAuth('/api/office/state');
      } else {
        let token = window.authToken || '';
        if (!token) {
          try {
            const tRes = await fetch('/api/token');
            if (tRes.ok) {
              const tj = await tRes.json();
              token = tj.token;
            }
          } catch (_) {}
        }
        const headers = token ? { Authorization: `Bearer ${token}` } : {};
        res = await fetch('/api/office/state', { headers });
      }
      if (!res || !res.ok) return;

      const state = await res.json();
      officeState = state;

      // Update CTX / Token counter in terminal footer
      const ctxEl = document.getElementById('dunder-ctx');
      if (ctxEl && state.overview) {
        const tokens = state.overview.today_tokens || 146000;
        ctxEl.textContent = `ctx ${(tokens / 1000).toFixed(0)}k/1000k (15%)`;
      }

      // If the user is viewing tasks or memory tab, update live
      if (activeTab === 'tasks' || activeTab === 'memory' || activeTab === 'activity') {
        renderTabContent(activeTab);
      }
    } catch (e) {
      console.warn('fetchOfficeState warning:', e);
    }
  }

  // ── Canvas Click Interaction ───────────────────────────────────────────────
  function setupCanvasInteraction() {
    if (!canvas) return;

    canvas.onclick = (e) => {
      const rect = canvas.getBoundingClientRect();
      const scaleX = MAP_W / rect.width;
      const scaleY = MAP_H / rect.height;
      const clickX = (e.clientX - rect.left) * scaleX;
      const clickY = (e.clientY - rect.top) * scaleY;

      // Find closest character within 24 pixels
      let closest = null;
      let minDist = 30;

      for (const char of characters) {
        const dx = char.x - clickX;
        const dy = char.y - 16 - clickY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        if (dist < minDist) {
          minDist = dist;
          closest = char;
        }
      }

      if (closest) {
        selectAgent(closest.id);
      }
    };
  }

  // ── Initialization Routine ─────────────────────────────────────────────────
  async function init() {
    canvas = document.getElementById('dunder-canvas');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    ctx.imageSmoothingEnabled = false;

    // Paint Boss Michael portrait in top card
    const bossCanvas = document.getElementById('boss-avatar');
    if (bossCanvas && window.PortraitArt && typeof window.PortraitArt.paintPortrait === 'function') {
      const bossCtx = bossCanvas.getContext('2d');
      window.PortraitArt.paintPortrait(bossCtx, 'michael', 1.5);
    }

    setupTabs();
    initTerminalView();
    setupQueueComposer();
    setupZoomControls();
    setupWindowActions();
    setupCanvasInteraction();
    renderRosterStrip();

    try {
      // 1. Load map JSON
      tmjData = await loadJson('/static/maps/office.tmj');

      // 2. Load the 3 tileset images in parallel
      const imgPromises = TILESET_DEFS.map(async (def) => {
        tilesetImages[def.name] = await loadImage(def.url);
      });
      await Promise.all(imgPromises);

      // 3. Pre-render background and foreground layers
      preRenderMap();

      // 4. Initialize character sprite frame canvases
      initCharacterSprites();

      // 5. Start animation loop
      if (animationFrameId) cancelAnimationFrame(animationFrameId);
      lastTime = performance.now();
      animationFrameId = requestAnimationFrame(render);

      // 6. Fetch live state from Mission Control
      await fetchOfficeState();
      pollInterval = setInterval(fetchOfficeState, 4000);
    } catch (err) {
      console.error('Office floor init error:', err);
    }
  }

  // ── Expose Public MCOffice API for Mission Control ─────────────────────────
  window.MCOffice = {
    wakeUp: function () {
      if (!canvas) {
        init();
        return;
      }
      const bossCanvas = document.getElementById('boss-avatar');
      if (bossCanvas && window.PortraitArt && typeof window.PortraitArt.paintPortrait === 'function') {
        const bossCtx = bossCanvas.getContext('2d');
        window.PortraitArt.paintPortrait(bossCtx, 'michael', 1.5);
      }
      if (!bgCanvas || !fgCanvas) {
        preRenderMap();
      }
      if (Object.keys(charFrameCanvases).length === 0) {
        initCharacterSprites();
      }
      renderRosterStrip();
      if (!animationFrameId) {
        lastTime = performance.now();
        animationFrameId = requestAnimationFrame(render);
      }
      fetchOfficeState();
    },
    onEvent: function (evt) {
      fetchOfficeState();
    },
    refresh: function () {
      this.wakeUp();
    },
    selectAgent: function (id) {
      selectAgent(id);
    },
    debug: function () {
      return { characters, selectedAgentId, activeTab, officeState };
    }
  };

  // Run on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
