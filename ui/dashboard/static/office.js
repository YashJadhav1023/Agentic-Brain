// office.js — Dunder Mifflin Retro Office & Command Center for Mission Control
// Exact retro pixel-art aesthetic matching munder-difflin with Dunder Mifflin window chrome,
// Tiled office floor map, procedural character sprites, speech bubbles, Command Center tabs,
// live terminal console, and bottom agent roster strip.

(function () {
  'use strict';

  function esc(s) {
    if (s == null) return '';
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  let isOfficeVisible = true;

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
      agentId: 'antigravity-account-1',
      isGod: true,
      status: 'idle',
      action: 'orchestrator',
      note: 'Architect (AG-1)',
      role: 'Architect (AG-1)',
      badge: 'GOD',
      x: 48,
      y: 56,
      dir: 'down',
      sitting: true,
      progress: 0,
      bubble: null,
      inRoster: true,
      waypoints: [
        { x: 48, y: 56, wait: 16, sit: true, bubble: null },
        { x: 80, y: 76, wait: 6, sit: false, bubble: 'Standup!' },
        { x: 48, y: 56, wait: 12, sit: true, bubble: null }
      ]
    },
    {
      id: 'jim',
      name: 'Jim',
      label: 'JIM',
      charName: 'jim',
      agentId: 'kiro-cli',
      isGod: false,
      status: 'idle',
      action: 'standby',
      note: 'Terminal & Test (Kiro)',
      role: 'Terminal & Test (Kiro)',
      badge: 'idle',
      x: 32,
      y: 204,
      dir: 'up',
      sitting: true,
      progress: 0,
      bubble: null,
      inRoster: true,
      waypoints: [
        { x: 32, y: 204, wait: 14, sit: true, bubble: null },
        { x: 70, y: 204, wait: 5, sit: false, bubble: 'CLI run' },
        { x: 32, y: 204, wait: 10, sit: true, bubble: null }
      ]
    },
    {
      id: 'pam',
      name: 'Pam',
      label: 'PAM',
      charName: 'pam',
      agentId: 'cline-account-1',
      isGod: false,
      status: 'idle',
      action: 'standby',
      note: 'Frontend & UI (Cline-1)',
      role: 'Frontend & UI (Cline-1)',
      badge: 'idle',
      x: 184,
      y: 242,
      dir: 'down',
      sitting: false,
      progress: 0,
      bubble: null,
      inRoster: true,
      waypoints: [
        { x: 184, y: 242, wait: 6, bubble: null },
        { x: 184, y: 190, wait: 4, bubble: 'UI design' },
        { x: 210, y: 150, wait: 5, bubble: 'Design review' },
        { x: 184, y: 190, wait: 3, bubble: null }
      ]
    },
    {
      id: 'kevin',
      name: 'Kevin',
      label: 'KEVIN',
      charName: 'kevin',
      agentId: 'cline-account-2',
      isGod: false,
      status: 'idle',
      action: 'standby',
      note: 'Components (Cline-2)',
      role: 'Components (Cline-2)',
      badge: 'idle',
      x: 250,
      y: 256,
      dir: 'right',
      sitting: false,
      progress: 0,
      bubble: null,
      inRoster: true,
      waypoints: [
        { x: 250, y: 256, wait: 5, bubble: null },
        { x: 270, y: 240, wait: 7, bubble: 'Components' },
        { x: 220, y: 256, wait: 4, bubble: 'Testing props' },
        { x: 120, y: 260, wait: 4, bubble: null }
      ]
    },
    {
      id: 'ryan',
      name: 'Ryan',
      label: 'RYAN',
      charName: 'ryan',
      agentId: 'antigravity-account-3',
      isGod: false,
      status: 'idle',
      action: 'standby',
      note: 'Implementation (AG-3)',
      role: 'Implementation (AG-3)',
      badge: 'idle',
      x: 160,
      y: 204,
      dir: 'up',
      sitting: true,
      progress: 0,
      bubble: null,
      inRoster: true,
      waypoints: [
        { x: 160, y: 204, wait: 12, sit: true, bubble: null },
        { x: 184, y: 180, wait: 5, sit: false, bubble: 'Code sync' },
        { x: 160, y: 204, wait: 10, sit: true, bubble: null }
      ]
    },
    {
      id: 'stanley',
      name: 'Stanley',
      label: 'STANLEY',
      charName: 'stanley',
      agentId: 'antigravity-account-2077',
      isGod: false,
      status: 'idle',
      action: 'standby',
      note: 'Deep Reasoning (AG-2077)',
      role: 'Deep Reasoning (AG-2077)',
      badge: 'idle',
      x: 264,
      y: 256,
      dir: 'left',
      sitting: false,
      progress: 0,
      bubble: null,
      inRoster: true,
      waypoints: [
        { x: 264, y: 256, wait: 8, bubble: null },
        { x: 210, y: 256, wait: 5, bubble: 'Deep reasoning' },
        { x: 264, y: 240, wait: 7, bubble: null }
      ]
    },
    {
      id: 'meredith',
      name: 'Meredith',
      label: 'MEREDITH',
      charName: 'meredith',
      agentId: 'cline-account-3',
      isGod: false,
      status: 'idle',
      action: 'standby',
      note: 'UI Styling (Cline-3)',
      role: 'UI Styling (Cline-3)',
      badge: 'idle',
      x: 224,
      y: 204,
      dir: 'up',
      sitting: true,
      progress: 0,
      bubble: null,
      inRoster: true,
      waypoints: [
        { x: 224, y: 204, wait: 14, sit: true, bubble: null },
        { x: 240, y: 170, wait: 5, sit: false, bubble: 'CSS polish' },
        { x: 224, y: 204, wait: 12, sit: true, bubble: null }
      ]
    },
    {
      id: 'dwight',
      name: 'Dwight',
      label: 'DWIGHT',
      charName: 'dwight',
      agentId: 'antigravity-account-2',
      isGod: false,
      status: 'idle',
      action: 'standby',
      note: 'Refactor & Review (AG-2)',
      role: 'Refactor & Review (AG-2)',
      badge: 'idle',
      x: 96,
      y: 204,
      dir: 'up',
      sitting: true,
      progress: 0,
      bubble: null,
      inRoster: true,
      waypoints: [
        { x: 96, y: 204, wait: 10, sit: true, bubble: null },
        { x: 96, y: 160, wait: 5, sit: false, bubble: 'Refactor review' },
        { x: 96, y: 204, wait: 10, sit: true, bubble: null }
      ]
    },
    {
      id: 'toby',
      name: 'Toby',
      label: 'TOBY',
      charName: 'toby',
      agentId: 'antigravity-account-2078',
      isGod: false,
      status: 'idle',
      action: 'standby',
      note: 'Governance & Spec (AG-2078)',
      role: 'Governance & Spec (AG-2078)',
      badge: 'idle',
      x: 320,
      y: 110,
      dir: 'up',
      sitting: true,
      progress: 0,
      bubble: null,
      inRoster: true,
      waypoints: [
        { x: 320, y: 110, wait: 20, sit: true, bubble: null },
        { x: 280, y: 110, wait: 6, sit: false, bubble: 'Governance check' },
        { x: 320, y: 110, wait: 15, sit: true, bubble: null }
      ]
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

  // ── Character Movement & Waypoint Logic ────────────────────────────────────
  function updateCharacterMotion(dt, timeS) {
    for (const char of characters) {
      if (!char.waypoints || char.waypoints.length === 0) continue;

      if (char.pauseTimer === undefined) {
        char.wpIndex = 0;
        char.pauseTimer = char.waypoints[0].wait || 3;
        char.targetX = char.waypoints[0].x;
        char.targetY = char.waypoints[0].y;
      }

      if (char.pauseTimer > 0) {
        char.pauseTimer -= dt;
        char.isWalking = false;
        continue;
      }

      const dx = char.targetX - char.x;
      const dy = char.targetY - char.y;
      const dist = Math.hypot(dx, dy);

      if (dist < 2) {
        char.x = char.targetX;
        char.y = char.targetY;
        char.isWalking = false;

        // Advance to next waypoint
        char.wpIndex = (char.wpIndex + 1) % char.waypoints.length;
        const nextWp = char.waypoints[char.wpIndex];
        char.targetX = nextWp.x;
        char.targetY = nextWp.y;
        char.pauseTimer = nextWp.wait || 4;
        if (nextWp.sit !== undefined) {
          char.sitting = nextWp.sit;
        }
        if (nextWp.bubble !== undefined && !char.hasTaskBubble) {
          char.bubble = nextWp.bubble;
        }
      } else {
        char.sitting = false;
        char.isWalking = true;
        const speed = 22; // smooth walking speed in pixels/second
        const step = Math.min(dist, speed * dt);
        char.x += (dx / dist) * step;
        char.y += (dy / dist) * step;
        if (Math.abs(dy) > Math.abs(dx)) {
          char.dir = dy < 0 ? 'up' : 'down';
        } else {
          char.dir = 'down';
        }
      }
    }
  }

  // ── Render Loop ────────────────────────────────────────────────────────────
  function render(timeMs) {
    if (!isOfficeVisible || document.hidden) {
      animationFrameId = null;
      return;
    }
    animationFrameId = requestAnimationFrame(render);
    const dt = Math.min((timeMs - lastTime) / 1000, 0.1);
    lastTime = timeMs;
    const timeS = timeMs / 1000;

    // Advance dynamic character motion
    updateCharacterMotion(dt, timeS);

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
      if (char.isWalking) {
        frameIndex = Math.floor(timeS * 4) % frameList.length;
      } else if (!char.sitting) {
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

    // Clock icon indicator for Michael's office wall
    let clockEl = document.getElementById('michael-wall-clock');
    if (!clockEl) {
      clockEl = document.createElement('div');
      clockEl.id = 'michael-wall-clock';
      clockEl.style.position = 'absolute';
      clockEl.style.left = (48 / MAP_W) * 100 + '%';
      clockEl.style.top = (28 / MAP_H) * 100 + '%';
      clockEl.innerHTML = `
        <div style="display:inline-flex; align-items:center; gap:4px; font-size:10px; font-weight:700;">
          <i class="fa-regular fa-clock" style="color:#B91C1C; font-size:12px;"></i>
          <span style="background:#FFF; border:1px solid #2B2118; padding:1px 4px; border-radius:3px;">idle</span>
          <i class="fa-regular fa-calendar-days" style="color:#B91C1C; font-size:12px;"></i>
        </div>
      `;
      container.appendChild(clockEl);
    }

    // Dynamic bubbles per character tracked above their current (x, y)
    const offset = Math.sin(timeS * 2) * 1.5;
    for (const char of characters) {
      const bubbleId = 'bubble-' + char.id;
      let el = document.getElementById(bubbleId);
      const bubbleText = char.bubble;

      if (!bubbleText) {
        if (el) el.style.display = 'none';
        continue;
      }

      if (!el) {
        el = document.createElement('div');
        el.id = bubbleId;
        container.appendChild(el);
      }

      el.style.display = 'block';
      el.textContent = bubbleText;
      el.className = 'dunder-bubble ' + (char.status === 'working' ? 'starting' : 'awaiting');
      el.style.left = (char.x / MAP_W) * 100 + '%';
      el.style.top = ((char.y - 18) / MAP_H) * 100 + '%';
      el.style.transform = `translate(-50%, calc(-100% + ${offset}px))`;
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
      const badgeClass = char.isGod ? 'god' : (char.status || 'idle');
      badgeSpan.className = 'agent-badge ' + badgeClass;
      badgeSpan.innerHTML = `<span class="status-sq ${char.status || 'idle'}"></span> ${char.isGod ? 'GOD' : (char.status || 'idle')}`;

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
      note.textContent = char.note || char.role || char.action;
      note.title = char.action || char.role;
      meta.appendChild(note);

      if (char.isGod) {
        const talkBtn = document.createElement('button');
        talkBtn.type = 'button';
        talkBtn.className = 'agent-talk-btn';
        talkBtn.innerHTML = '<i class="fa-solid fa-bolt"></i> auto';
        talkBtn.title = 'Michael auto-routes to best worker across all accounts';
        talkBtn.onclick = (e) => {
          e.stopPropagation();
          selectAgent(char.id);
          const input = document.getElementById('dunder-queue-input');
          if (input) input.focus();
        };
        meta.appendChild(talkBtn);
      } else {
        const progBar = document.createElement('div');
        progBar.className = 'agent-progress-bar';
        const progFill = document.createElement('div');
        progFill.className = 'agent-progress-fill';
        progFill.style.width = (char.status === 'working' ? (char.progress || 70) : 0) + '%';
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
    updateRosterScrollButtons();
  }

  function updateRosterScrollButtons() {
    const strip = document.getElementById('dunder-roster-strip');
    const prevBtn = document.getElementById('dunder-roster-prev');
    const nextBtn = document.getElementById('dunder-roster-next');
    if (!strip) return;
    if (prevBtn) prevBtn.classList.toggle('hidden', strip.scrollLeft <= 5);
    if (nextBtn) nextBtn.classList.toggle('hidden', strip.scrollLeft + strip.clientWidth >= strip.scrollWidth - 5);
  }

  let rosterControlsInitialized = false;
  function setupRosterControls() {
    if (rosterControlsInitialized) return;
    const strip = document.getElementById('dunder-roster-strip');
    const prevBtn = document.getElementById('dunder-roster-prev');
    const nextBtn = document.getElementById('dunder-roster-next');
    if (strip) {
      strip.addEventListener('scroll', updateRosterScrollButtons, { passive: true });
      rosterControlsInitialized = true;
    }
    if (prevBtn && strip) {
      prevBtn.onclick = () => {
        strip.scrollBy({ left: -260, behavior: 'smooth' });
      };
    }
    if (nextBtn && strip) {
      nextBtn.onclick = () => {
        strip.scrollBy({ left: 260, behavior: 'smooth' });
      };
    }
    updateRosterScrollButtons();
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
        bossStatus.innerHTML = `<span class="status-sq idle"></span> idle &nbsp; Michael (All Accounts Smart Router)`;
      } else {
        bossStatus.innerHTML = `<span class="status-sq ${esc(char.status)}"></span> ${esc(char.status)} &nbsp; ${esc(char.name)} (${esc(char.role)})`;
      }
    }

    // Update input placeholder to guide the operator
    const input = document.getElementById('dunder-queue-input');
    if (input) {
      if (char.isGod) {
        input.placeholder = 'Instruct Michael (Auto-routes to best worker across all accounts)...';
      } else {
        input.placeholder = `Direct task to ${char.name} (${char.agentId})...`;
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
    <p class="term-bullet">● On it — checking status across all 9 agent accounts.</p>
    <p class="term-muted">Ran 1 shell command</p>
    <p class="term-bullet" style="margin-top: 6px;">● Sent a status query to all 9 agents — <strong>Jim (Kiro), Pam (Cline-1), Kevin (Cline-2), Meredith (Cline-3), Ryan (AG-3), Stanley (AG-2077), Dwight (AG-2), Toby (AG-2078), and Michael (AG-1)</strong>. Smart router distributes tasks evenly across all accounts.</p>
    <p class="term-muted" style="margin-top: 4px;">Replies land in my inbox — I'll collect them and give you a consolidated rundown as they come in.</p>
    <p class="term-baked">* Office floor synced with Shared Brain &amp; Mission Control</p>
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
        const agentName = t.agent || t.assigned_to || t.account || 'unassigned';
        const isRunning = t.stage === 'running' || t.stage === 'in-progress' || t.stage === 'in_progress';
        html += `
          <div style="background:#FAF2E3; border:1px solid #D9CEB8; padding:6px 8px; margin:4px 0; border-radius:3px; color:#231C16;">
            <div style="display:flex; justify-content:space-between; font-weight:700;">
              <span style="color:#B45309;">#${esc(t.id || 'task')}</span>
              <span style="color:${isRunning ? '#16A34A' : '#B45309'}; text-transform:uppercase;">${esc(t.stage || 'queued')}</span>
            </div>
            <div style="color:#231C16; margin-top:2px; font-weight:600;">${esc(t.title || 'Untitled task')}</div>
            <div style="color:#7A6F62; font-size:10px; margin-top:2px;">Agent: <strong style="color:#1F1914;">${esc(agentName)}</strong> · Stage: ${esc(t.stage || 'pending')}</div>
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
            <div style="color:#1D4ED8; font-weight:700;">${esc(m.title || m.id || '')}</div>
            <div style="color:#4B3F35; font-size:11px; margin-top:2px;">${esc(m.summary || m.snippet || '')}</div>
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
        const matchedChar = characters.find(c => c.agentId && c.agentId.toLowerCase() === (a.id || '').toLowerCase());
        const charLabel = matchedChar ? `(${matchedChar.name} · ${matchedChar.role})` : '';
        const isOffline = a.status === 'offline' || a.status === 'disabled';
        const isWorking = a.status === 'working';
        html += `
          <div style="display:flex; justify-content:space-between; padding:5px 0; border-bottom:1px solid #E2D7C2; color:#231C16; font-size:11px;">
            <span style="font-weight:700; color:#1F1914;">${esc(a.id)} <span style="font-weight:400; color:#6B5F54;">${esc(charLabel)}</span></span>
            <span style="color:#6B5F54; font-size:10px;">${esc(a.kind || 'agent')}</span>
            <span style="color:${isOffline ? '#DC2626' : (isWorking ? '#16A34A' : '#2F6F4E')}; font-weight:700;">[${esc(a.status)}]</span>
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
            <span style="color:#7C3AED; font-weight:700;">${esc(f.type || 'flow')}</span>: 
            <span style="color:#1F1914;">${esc(f.from_agent || 'orchestrator')} ➔ ${esc(f.to_agent || 'worker')}</span>
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
      const targetChar = characters.find((c) => c.id === selectedAgentId);
      const isAutoRoute = !targetChar || targetChar.isGod;
      const targetAgent = isAutoRoute ? null : targetChar.agentId;
      const targetDesc = isAutoRoute ? 'Smart Router (All Accounts)' : `${targetChar.name} (${targetChar.agentId})`;

      if (logsEl) {
        const p1 = document.createElement('p');
        p1.className = 'term-cmd-bar';
        p1.style.marginTop = '8px';
        p1.textContent = `> ${text}`;
        logsEl.appendChild(p1);

        const p2 = document.createElement('p');
        p2.className = 'term-bullet';
        p2.style.marginTop = '4px';
        p2.innerHTML = `<span style="color:#22C55E;">●</span> Dispatching instruction to <strong>${esc(targetDesc)}</strong>...`;
        logsEl.appendChild(p2);
        logsEl.scrollTop = logsEl.scrollHeight;
      }

      const payload = {
        instruction: text,
        agent: targetAgent,
        auto_execute: true
      };

      const dispatchHeaders = { 'Content-Type': 'application/json' };
      if (window.authToken) {
        dispatchHeaders['Authorization'] = `Bearer ${window.authToken}`;
      }
      const dispatchCall = typeof window.fetchWithAuth === 'function'
        ? window.fetchWithAuth('/api/dispatch', { method: 'POST', body: payload })
        : fetch('/api/dispatch', {
            method: 'POST',
            headers: dispatchHeaders,
            body: JSON.stringify(payload)
          });

      dispatchCall
        .then(async (res) => {
          if (!res.ok) {
            const errText = await res.text();
            throw new Error(`Dispatch failed HTTP ${res.status}: ${errText}`);
          }
          return res.json();
        })
        .then((data) => {
          if (logsEl) {
            const pSuccess = document.createElement('p');
            pSuccess.className = 'term-muted';
            const assigned = data.task?.assigned_agent || data.task?.assigned_account || targetAgent || 'Smart Routed';
            pSuccess.innerHTML = `✓ Dispatched as <strong>#${esc(data.task_id || 'task')}</strong> &rarr; Assigned to <strong>${esc(assigned)}</strong>`;
            logsEl.appendChild(pSuccess);
            logsEl.scrollTop = logsEl.scrollHeight;
          }
          fetchOfficeState();
        })
        .catch((err) => {
          console.error('Dispatch error:', err);
          if (logsEl) {
            const pErr = document.createElement('p');
            pErr.style.color = '#DC2626';
            pErr.style.fontSize = '11px';
            pErr.textContent = `Dispatch error: ${err.message}`;
            logsEl.appendChild(pErr);
            logsEl.scrollTop = logsEl.scrollHeight;
          }
        });
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

      // Map backend agents by ID for fast lookup
      const agentsById = {};
      if (state.agents && Array.isArray(state.agents)) {
        for (const a of state.agents) {
          if (a && a.id) {
            agentsById[a.id.toLowerCase()] = a;
          }
        }
      }

      // Active / in-flight tasks from the swarm & mission control
      const allTasks = state.tasks || [];
      const activeStages = ['running', 'queued', 'delivered', 'review', 'in_progress', 'in-progress'];
      const activeTasks = allTasks.filter((t) => activeStages.includes((t.stage || '').toLowerCase()));

      // Synchronize each character on the office floor
      for (const char of characters) {
        const cAgentId = (char.agentId || '').toLowerCase();
        const agentObj = agentsById[cAgentId];

        // Find active task assigned to this agent
        let task = activeTasks.find((t) => {
          const tAgent = (t.agent || t.assigned_to || t.account || '').toLowerCase();
          if (!tAgent) return false;
          if (tAgent === cAgentId) return true;
          if (cAgentId && tAgent.includes(cAgentId)) return true;
          if (char.isGod && (tAgent.includes('orchestrator') || tAgent.includes('architect') || tAgent === 'antigravity-account-1')) return true;
          return false;
        });

        // Also check if agentObj has a current_task_id
        if (!task && agentObj && agentObj.current_task_id) {
          task = allTasks.find(t => t.id === agentObj.current_task_id);
        }

        if (task) {
          const stage = (task.stage || '').toLowerCase();
          char.status = (stage === 'running' || stage === 'in-progress' || stage === 'in_progress') ? 'working' : (stage === 'review' ? 'review' : 'working');
          char.action = task.title || 'executing task';
          char.bubble = task.title.length > 22 ? task.title.slice(0, 20) + '…' : task.title;
          char.hasTaskBubble = true;
          char.progress = (stage === 'running') ? 75 : (stage === 'review' ? 90 : 35);
          char.note = (char.status === 'working' ? '⚡ ' : '') + (task.title.length > 20 ? task.title.slice(0, 18) + '…' : task.title);
        } else {
          char.hasTaskBubble = false;
          if (agentObj) {
            const rawStatus = (agentObj.status || 'idle').toLowerCase();
            if (rawStatus === 'offline' || rawStatus === 'disabled') {
              char.status = 'offline';
              char.action = 'offline';
              char.bubble = 'offline';
              char.note = `${char.role} [offline]`;
              char.progress = 0;
            } else if (rawStatus === 'blocked' || rawStatus === 'degraded' || rawStatus === 'rate_limited') {
              char.status = 'blocked';
              char.action = 'blocked';
              char.bubble = 'blocked';
              char.note = `${char.role} [blocked]`;
              char.progress = 0;
            } else {
              char.status = 'idle';
              char.action = char.isGod ? 'orchestrator' : 'standby';
              char.bubble = null;
              char.note = char.role;
              char.progress = 0;
            }
          } else {
            char.status = 'idle';
            char.action = char.isGod ? 'orchestrator' : 'standby';
            char.bubble = null;
            char.note = char.role;
            char.progress = 0;
          }
        }
      }

      // Re-render bottom roster strip with synchronized badges and roles
      renderRosterStrip();

      // Update boss card if selected
      const selectedChar = characters.find(c => c.id === selectedAgentId);
      if (selectedChar) {
        const bossStatus = document.querySelector('.boss-status');
        if (bossStatus) {
          if (selectedChar.isGod) {
            bossStatus.innerHTML = `<span class="status-sq idle"></span> idle &nbsp; Michael (All Accounts Smart Router)`;
          } else {
            bossStatus.innerHTML = `<span class="status-sq ${selectedChar.status}"></span> ${selectedChar.status} &nbsp; ${selectedChar.name} (${selectedChar.role})`;
          }
        }
      }

      // Update tab contents if on tasks/memory/workers/activity
      if (activeTab === 'tasks' || activeTab === 'memory' || activeTab === 'workers' || activeTab === 'activity') {
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
    setupRosterControls();

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
      if (pollInterval) clearInterval(pollInterval);
      pollInterval = setInterval(fetchOfficeState, 4000);
    } catch (err) {
      console.error('Office floor init error:', err);
    }
  }

  // ── Expose Public MCOffice API for Mission Control ─────────────────────────
  window.MCOffice = {
    sleep: function () {
      isOfficeVisible = false;
      if (animationFrameId) {
        cancelAnimationFrame(animationFrameId);
        animationFrameId = null;
      }
      if (pollInterval) {
        clearInterval(pollInterval);
        pollInterval = null;
      }
    },
    wakeUp: function () {
      isOfficeVisible = true;
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
      setupRosterControls();
      if (!animationFrameId) {
        lastTime = performance.now();
        animationFrameId = requestAnimationFrame(render);
      }
      if (!pollInterval) {
        pollInterval = setInterval(fetchOfficeState, 4000);
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

  document.addEventListener('visibilitychange', () => {
    const officeTab = document.getElementById('tab-office');
    const isTabActive = officeTab && !officeTab.classList.contains('hidden');
    if (document.hidden || !isTabActive) {
      if (window.MCOffice && typeof window.MCOffice.sleep === 'function') {
        window.MCOffice.sleep();
      }
    } else {
      if (window.MCOffice && typeof window.MCOffice.wakeUp === 'function') {
        window.MCOffice.wakeUp();
      }
    }
  });

  // Run on DOM ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
