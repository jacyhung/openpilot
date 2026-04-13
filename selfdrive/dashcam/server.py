"""Dashcam video browser — aiohttp server on port 8082.

Routes are read from /data/media/0/realdata.  Each directory whose name
matches  {bootcount}--{routehash}--{segnum}  is a one-minute segment.
The server groups segments into routes and exposes them via a JSON API.
The web UI is a single-page app embedded in INDEX_HTML below.
"""

import asyncio
import os
import re
import time
from datetime import datetime
from pathlib import Path

from aiohttp import web

REALDATA = Path(os.environ.get("DASHCAM_DATA_DIR", "/data/media/0/realdata"))
PORT = int(os.environ.get("DASHCAM_PORT", "8082"))

# Directory names: {routePrefix}--{segNum}
# routePrefix is itself {bootHex}--{routeHash}
SEGMENT_PATTERN = re.compile(r'^([0-9a-f]+--[0-9a-f]+)--(\d+)$')
ROUTE_ID_PATTERN = re.compile(r'^[0-9a-f]+--[0-9a-f]+$')

CAMERA_FILES: dict[str, str] = {
    'q': 'qcamera.ts',
    'f': 'fcamera.hevc',
    'd': 'dcamera.hevc',
    'e': 'ecamera.hevc',
}
CAMERA_MIME: dict[str, str] = {
    'q': 'video/mp2t',
    'f': 'video/hevc',
    'd': 'video/hevc',
    'e': 'video/hevc',
}

_cache: dict = {}
_cache_time: float = 0.0
CACHE_TTL = 20.0  # seconds


def _scan_routes() -> list[dict]:
    """Scan REALDATA and group segment directories into routes.  Results are
    cached for CACHE_TTL seconds to avoid hammering the filesystem."""
    global _cache, _cache_time
    now = time.monotonic()
    if _cache and now - _cache_time < CACHE_TTL:
        return sorted(_cache.values(), key=lambda r: -r['mtime'])

    routes: dict[str, dict] = {}

    if not REALDATA.exists():
        _cache = {}
        _cache_time = now
        return []

    for entry in os.scandir(REALDATA):
        if not entry.is_dir():
            continue
        m = SEGMENT_PATTERN.match(entry.name)
        if not m:
            continue
        route_id = m.group(1)
        seg_num = int(m.group(2))
        mtime = entry.stat().st_mtime

        if route_id not in routes:
            routes[route_id] = {
                'id': route_id,
                'mtime': mtime,
                'segments': [],
            }
        else:
            if mtime > routes[route_id]['mtime']:
                routes[route_id]['mtime'] = mtime

        routes[route_id]['segments'].append(seg_num)

    for r in routes.values():
        r['segments'].sort()
        r['seg_count'] = len(r['segments'])
        r['duration_min'] = r['seg_count']
        dt = datetime.fromtimestamp(r['mtime'])
        r['date'] = dt.strftime('%b %d, %Y')
        r['time'] = dt.strftime('%I:%M %p')

    _cache = routes
    _cache_time = now
    return sorted(routes.values(), key=lambda r: -r['mtime'])


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type='text/html', charset='utf-8')


async def handle_api_routes(request: web.Request) -> web.Response:
    loop = asyncio.get_event_loop()
    routes = await loop.run_in_executor(None, _scan_routes)
    listing = [
        {
            'id': r['id'],
            'date': r['date'],
            'time': r['time'],
            'mtime': r['mtime'],
            'seg_count': r['seg_count'],
            'duration_min': r['duration_min'],
        }
        for r in routes
    ]
    return web.json_response(listing)


async def handle_api_route(request: web.Request) -> web.Response:
    route_id = request.match_info['route_id']
    if not ROUTE_ID_PATTERN.match(route_id):
        raise web.HTTPBadRequest()
    loop = asyncio.get_event_loop()
    routes = await loop.run_in_executor(None, _scan_routes)
    for r in routes:
        if r['id'] == route_id:
            return web.json_response(r)
    raise web.HTTPNotFound()


async def handle_video(request: web.Request) -> web.Response:
    route_id = request.match_info['route_id']
    camera = request.match_info.get('camera', 'q')

    try:
        segment = int(request.match_info['segment'])
    except (ValueError, KeyError):
        raise web.HTTPBadRequest()

    if not ROUTE_ID_PATTERN.match(route_id):
        raise web.HTTPBadRequest()
    if camera not in CAMERA_FILES:
        raise web.HTTPBadRequest()
    if segment < 0 or segment > 99999:
        raise web.HTTPBadRequest()

    # Resolve paths and check for traversal
    realdata_resolved = REALDATA.resolve()
    seg_dir = (REALDATA / f"{route_id}--{segment}").resolve()
    video_path = (seg_dir / CAMERA_FILES[camera]).resolve()

    if not str(seg_dir).startswith(str(realdata_resolved) + os.sep):
        raise web.HTTPForbidden()

    if not video_path.exists():
        raise web.HTTPNotFound()

    return web.FileResponse(
        path=video_path,
        headers={'Content-Type': CAMERA_MIME[camera]},
        chunk_size=131072,
    )


# ---------------------------------------------------------------------------
# Embedded single-page UI
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Dashcam</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: { extend: { fontFamily: { mono: ['ui-monospace','SFMono-Regular','Menlo','monospace'] } } }
    }
  </script>
  <style>
    html,body{height:100%;margin:0;}
    ::-webkit-scrollbar{width:4px;height:4px;}
    ::-webkit-scrollbar-track{background:transparent;}
    ::-webkit-scrollbar-thumb{background:#3f3f46;border-radius:2px;}
    ::-webkit-scrollbar-thumb:hover{background:#52525b;}
    .seg-btn{transition:background 0.1s,color 0.1s;}
    .seg-btn.active{background:#22c55e!important;color:#000!important;font-weight:600;}
    .route-card{transition:border-color 0.15s,background 0.15s;}
    .route-card.selected{border-color:#22c55e!important;}
    video{background:#000;display:block;}
  </style>
</head>
<body class="bg-zinc-950 text-zinc-100" style="height:100dvh;display:flex;flex-direction:column;overflow:hidden;">

  <!-- ── Header ── -->
  <header class="flex-shrink-0 flex items-center gap-3 px-4 h-12 border-b border-zinc-800 bg-zinc-950">
    <span class="text-green-500 text-base leading-none select-none">⬤</span>
    <span class="font-semibold tracking-tight">Dashcam</span>
    <div class="flex-1"></div>
    <button
      onclick="refreshRoutes()"
      class="text-xs text-zinc-400 hover:text-zinc-100 border border-zinc-700 hover:border-zinc-500 rounded-md px-3 py-1.5 transition"
    >↺ Refresh</button>
  </header>

  <!-- ── Body ── -->
  <div style="flex:1;display:flex;min-height:0;overflow:hidden;">

    <!-- ── Sidebar ── -->
    <aside style="width:280px;flex-shrink:0;display:flex;flex-direction:column;border-right:1px solid #27272a;overflow:hidden;">

      <div class="p-2.5 border-b border-zinc-800">
        <input
          id="search"
          type="search"
          placeholder="Filter routes…"
          oninput="filterRoutes(this.value)"
          class="w-full bg-zinc-900 border border-zinc-800 hover:border-zinc-700 focus:border-zinc-600 rounded-lg px-3 py-2 text-sm text-zinc-100 placeholder-zinc-600 outline-none transition"
        >
      </div>

      <div id="route-count" class="px-3 py-1.5 text-xs text-zinc-600 border-b border-zinc-800"></div>

      <div id="route-list" style="flex:1;overflow-y:auto;" class="p-2 space-y-1">
        <p class="text-center text-zinc-600 text-sm py-10">Loading…</p>
      </div>
    </aside>

    <!-- ── Player panel ── -->
    <div style="flex:1;display:flex;flex-direction:column;min-width:0;overflow:hidden;">

      <!-- Empty state -->
      <div id="empty-state" style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;color:#3f3f46;">
        <svg xmlns="http://www.w3.org/2000/svg" style="width:56px;height:56px;" fill="none" viewBox="0 0 24 24" stroke="currentColor" stroke-width="0.8">
          <path stroke-linecap="round" stroke-linejoin="round" d="M15 10l4.553-2.069A1 1 0 0121 8.882v6.236a1 1 0 01-1.447.894L15 14M4 8h8a2 2 0 012 2v4a2 2 0 01-2 2H4a2 2 0 01-2-2v-4a2 2 0 012-2z"/>
        </svg>
        <p class="text-sm">Select a route to play</p>
      </div>

      <!-- Player (hidden until route selected) -->
      <div id="player-panel" style="display:none;flex:1;flex-direction:column;min-height:0;">

        <!-- Video area -->
        <div style="flex:1;background:#000;display:flex;align-items:center;justify-content:center;min-height:0;overflow:hidden;">
          <video
            id="video"
            controls
            playsinline
            style="max-height:100%;max-width:100%;width:100%;"
            onended="onVideoEnded()"
          ></video>
        </div>

        <!-- Info strip -->
        <div class="flex-shrink-0 bg-zinc-900 border-t border-zinc-800" style="padding:12px 16px;display:flex;flex-direction:column;gap:10px;">

          <!-- Route metadata row -->
          <div style="display:flex;align-items:flex-start;gap:12px;">
            <div style="min-width:0;flex:1;">
              <div id="route-date-display" class="text-sm font-medium leading-tight"></div>
              <div id="route-id-display" class="font-mono text-xs text-zinc-500 truncate mt-0.5"></div>
            </div>
            <div id="route-stats" class="text-xs text-zinc-500 flex-shrink-0 text-right"></div>
          </div>

          <!-- Segment selector -->
          <div>
            <div class="text-xs text-zinc-600 mb-1.5 uppercase tracking-wide" style="font-size:10px;">Segments</div>
            <div id="seg-list" style="display:flex;flex-wrap:wrap;gap:4px;max-height:80px;overflow-y:auto;"></div>
          </div>

          <!-- Download row -->
          <div>
            <div class="text-xs text-zinc-600 mb-1.5 uppercase tracking-wide" style="font-size:10px;">Download segment</div>
            <div id="download-links" style="display:flex;flex-wrap:wrap;gap:6px;"></div>
          </div>

        </div>
      </div>

    </div>
  </div>

<script>
let allRoutes = [];
let currentRoute = null;
let currentSegIdx = 0;
let currentSegments = [];

async function loadRoutes() {
  try {
    const resp = await fetch('/api/routes');
    if (!resp.ok) throw new Error(resp.status);
    allRoutes = await resp.json();
    renderRouteList(allRoutes);
    document.getElementById('route-count').textContent =
      `${allRoutes.length} route${allRoutes.length !== 1 ? 's' : ''}`;
  } catch (e) {
    document.getElementById('route-list').innerHTML =
      `<p class="text-center text-red-500 text-sm py-10">Failed to load routes.</p>`;
  }
}

function refreshRoutes() {
  document.getElementById('route-list').innerHTML =
    `<p class="text-center text-zinc-600 text-sm py-10">Loading…</p>`;
  loadRoutes();
}

function renderRouteList(routes) {
  const list = document.getElementById('route-list');
  if (!routes.length) {
    list.innerHTML = `<p class="text-center text-zinc-600 text-sm py-10">No routes found.</p>`;
    return;
  }
  list.innerHTML = routes.map(r => {
    const sel = currentRoute && currentRoute.id === r.id ? 'selected' : '';
    return `<div
      class="route-card ${sel} border border-zinc-800 hover:border-zinc-600 bg-zinc-900 hover:bg-zinc-800 rounded-lg p-3 cursor-pointer select-none"
      onclick="selectRoute('${r.id}')"
    >
      <div style="display:flex;align-items:flex-start;justify-content:space-between;gap:8px;">
        <div style="min-width:0;">
          <div class="text-sm font-medium leading-tight">${r.date}</div>
          <div class="text-xs text-zinc-500 leading-tight mt-0.5">${r.time}</div>
          <div class="font-mono text-zinc-600 truncate mt-1" style="font-size:10px;">${r.id}</div>
        </div>
        <div style="flex-shrink:0;text-align:right;">
          <div class="text-xs text-zinc-400">${r.seg_count} seg</div>
          <div class="text-zinc-600" style="font-size:10px;">~${r.duration_min}m</div>
        </div>
      </div>
    </div>`;
  }).join('');
}

function filterRoutes(q) {
  q = q.toLowerCase();
  const filtered = q
    ? allRoutes.filter(r => r.id.includes(q) || r.date.toLowerCase().includes(q) || r.time.toLowerCase().includes(q))
    : allRoutes;
  renderRouteList(filtered);
  const cnt = document.getElementById('route-count');
  cnt.textContent = q
    ? `${filtered.length} of ${allRoutes.length} routes`
    : `${allRoutes.length} route${allRoutes.length !== 1 ? 's' : ''}`;
}

async function selectRoute(routeId) {
  let route;
  try {
    const resp = await fetch(`/api/route/${routeId}`);
    if (!resp.ok) throw new Error(resp.status);
    route = await resp.json();
  } catch {
    return;
  }

  currentRoute = route;
  currentSegments = route.segments;
  currentSegIdx = 0;

  // Show player
  document.getElementById('empty-state').style.display = 'none';
  const pp = document.getElementById('player-panel');
  pp.style.display = 'flex';
  pp.style.flexDirection = 'column';

  // Metadata
  document.getElementById('route-date-display').textContent = `${route.date} · ${route.time}`;
  document.getElementById('route-id-display').textContent = route.id;
  document.getElementById('route-stats').textContent = `${route.seg_count} segments · ~${route.duration_min} min`;

  renderSegButtons();
  renderRouteList(allRoutes.filter(r => {
    const q = document.getElementById('search').value.toLowerCase();
    return !q || r.id.includes(q) || r.date.toLowerCase().includes(q) || r.time.toLowerCase().includes(q);
  }));
  playSegment(0);
}

function renderSegButtons() {
  const container = document.getElementById('seg-list');
  container.innerHTML = currentSegments.map((segNum, idx) => {
    const active = idx === currentSegIdx ? 'active' : '';
    return `<button
      id="segbtn-${segNum}"
      class="seg-btn ${active} bg-zinc-800 hover:bg-zinc-700 text-zinc-300 rounded font-mono"
      style="font-size:11px;padding:3px 7px;"
      onclick="clickSegment(${idx})"
    >${String(segNum).padStart(2, '0')}</button>`;
  }).join('');
}

function clickSegment(idx) {
  currentSegIdx = idx;
  playSegment(idx);
}

function playSegment(idx) {
  currentSegIdx = idx;
  const segNum = currentSegments[idx];
  const video = document.getElementById('video');

  // Update button highlights
  document.querySelectorAll('[id^="segbtn-"]').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById(`segbtn-${segNum}`);
  if (btn) {
    btn.classList.add('active');
    btn.scrollIntoView({behavior: 'smooth', block: 'nearest', inline: 'nearest'});
  }

  // Play
  video.src = `/video/${currentRoute.id}/${segNum}/q`;
  video.play().catch(() => {});

  updateDownloadLinks(segNum);
}

function updateDownloadLinks(segNum) {
  const cams = [
    {key:'q', label:'Road 360p', ext:'ts'},
    {key:'f', label:'Front', ext:'hevc'},
    {key:'d', label:'Driver', ext:'hevc'},
    {key:'e', label:'Wide', ext:'hevc'},
  ];
  document.getElementById('download-links').innerHTML = cams.map(c =>
    `<a
      href="/video/${currentRoute.id}/${segNum}/${c.key}"
      download="${currentRoute.id}--${segNum}-${c.key}.${c.ext}"
      class="text-xs border border-zinc-700 hover:border-zinc-500 text-zinc-400 hover:text-zinc-200 rounded-md transition"
      style="padding:4px 10px;text-decoration:none;"
    >↓ ${c.label}</a>`
  ).join('');
}

function onVideoEnded() {
  if (currentRoute && currentSegIdx < currentSegments.length - 1) {
    playSegment(currentSegIdx + 1);
  }
}

loadRoutes();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import logging
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

    app = web.Application()
    app.router.add_get('/', handle_index)
    app.router.add_get('/api/routes', handle_api_routes)
    app.router.add_get('/api/route/{route_id}', handle_api_route)
    app.router.add_get('/video/{route_id}/{segment}', handle_video)
    app.router.add_get('/video/{route_id}/{segment}/{camera}', handle_video)

    web.run_app(app, host='0.0.0.0', port=PORT, access_log=None)


if __name__ == '__main__':
    main()
