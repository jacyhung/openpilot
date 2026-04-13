"""Dashcam video browser — aiohttp server on port 8082.

Routes are read from /data/media/0/realdata.  Each directory whose name
matches  {bootcount}--{routehash}--{segnum}  is a one-minute segment.
The server groups segments into routes and exposes them via a JSON API.

Video is remuxed by ffmpeg (HEVC/H.264 → regular MP4) and served with full
byte-range support so browsers and iOS can scrub freely.
"""

import asyncio
import collections
import io
import os
import re
import tempfile
import time
import wave
from pathlib import Path

from aiohttp import web

REALDATA = Path(os.environ.get("DASHCAM_DATA_DIR", "/data/media/0/realdata"))
PORT = int(os.environ.get("DASHCAM_PORT", "8082"))

# LRU cache for remuxed MP4 bytes — avoids re-running ffmpeg on every Range
# sub-request. Keyed on (route_id, segment, camera). 3 entries ≈ 75-225 MB max.
MAX_MP4_CACHE = 3
_mp4_cache: collections.OrderedDict[tuple[str, int, str], bytes] = collections.OrderedDict()
_mp4_locks: dict[tuple[str, int, str], asyncio.Lock] = {}

SEGMENT_PATTERN = re.compile(r'^([0-9a-f]+--[0-9a-f]+)--(\d+)$')
ROUTE_ID_PATTERN = re.compile(r'^[0-9a-f]+--[0-9a-f]+$')

# camera key → (filename, optional ffmpeg -f input-format hint)
CAMERAS: dict[str, tuple[str, str | None]] = {
    'q': ('qcamera.ts',   None),     # MPEG-TS container, ffmpeg auto-detects
    'f': ('fcamera.hevc', 'hevc'),   # raw HEVC bitstream
    'd': ('dcamera.hevc', 'hevc'),
    'e': ('ecamera.hevc', 'hevc'),
}
CAMERA_LABEL: dict[str, str] = {
    'q': 'Road (360p)',
    'f': 'Front',
    'd': 'Driver',
    'e': 'Wide',
}

_cache: dict = {}
_cache_time: float = 0.0
CACHE_TTL = 20.0


def _scan_routes() -> list[dict]:
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
            routes[route_id] = {'id': route_id, 'mtime': mtime, 'segments': []}
        elif mtime > routes[route_id]['mtime']:
            routes[route_id]['mtime'] = mtime
        routes[route_id]['segments'].append(seg_num)

    for r in routes.values():
        r['segments'].sort()
        r['seg_count'] = len(r['segments'])

    _cache = routes
    _cache_time = now
    return sorted(routes.values(), key=lambda r: -r['mtime'])


def _validate_video_path(route_id: str, segment: int, camera: str) -> Path:
    """Return resolved video Path or raise an appropriate HTTP error."""
    if not ROUTE_ID_PATTERN.match(route_id):
        raise web.HTTPBadRequest()
    if camera not in CAMERAS:
        raise web.HTTPBadRequest()
    if not (0 <= segment <= 99999):
        raise web.HTTPBadRequest()

    realdata_resolved = REALDATA.resolve()
    seg_dir = (REALDATA / f"{route_id}--{segment}").resolve()
    filename, _ = CAMERAS[camera]
    video_path = (seg_dir / filename).resolve()

    if not str(seg_dir).startswith(str(realdata_resolved) + os.sep):
        raise web.HTTPForbidden()
    if not video_path.exists():
        raise web.HTTPNotFound()
    return video_path


# ---------------------------------------------------------------------------
# Request handlers
# ---------------------------------------------------------------------------

async def handle_index(request: web.Request) -> web.Response:
    return web.Response(text=INDEX_HTML, content_type='text/html', charset='utf-8')


async def handle_api_routes(request: web.Request) -> web.Response:
    loop = asyncio.get_event_loop()
    routes = await loop.run_in_executor(None, _scan_routes)
    return web.json_response([
        {'id': r['id'], 'mtime': r['mtime'], 'seg_count': r['seg_count']}
        for r in routes
    ])


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


async def handle_stream(request: web.Request) -> web.Response:
    """Remux HEVC/H.264 → regular MP4 via ffmpeg, serve with full Range support.

    The remuxed MP4 is cached in memory (LRU, up to MAX_MP4_CACHE entries) so
    that subsequent Range sub-requests (including the iOS bytes=0-1 probe) all
    serve from the *same* binary.  Without caching, every request would re-run
    ffmpeg, producing a different file each time — the moov from request #1
    wouldn't match the mdat from request #2, breaking seeking and iOS playback.
    """
    route_id = request.match_info['route_id']
    camera   = request.match_info.get('camera', 'q')
    try:
        segment = int(request.match_info['segment'])
    except (ValueError, KeyError):
        raise web.HTTPBadRequest()

    video_path = _validate_video_path(route_id, segment, camera)
    _, fmt = CAMERAS[camera]

    cache_key = (route_id, segment, camera)

    # Acquire a per-key lock so concurrent Range requests wait for a single
    # ffmpeg run instead of each spawning their own.
    if cache_key not in _mp4_locks:
        _mp4_locks[cache_key] = asyncio.Lock()
    lock = _mp4_locks[cache_key]

    async with lock:
        if cache_key in _mp4_cache:
            # Move to end (most-recently used)
            _mp4_cache.move_to_end(cache_key)
            mp4_bytes = _mp4_cache[cache_key]
        else:
            # Run ffmpeg once; cache the result
            tmp_fd, tmpname = tempfile.mkstemp(suffix='.mp4')
            os.close(tmp_fd)
            try:
                cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
                if fmt:
                    cmd += ['-f', fmt]
                cmd += ['-i', str(video_path), '-c:v', 'copy']
                if fmt == 'hevc':
                    cmd += ['-tag:v', 'hvc1']
                if camera == 'q':
                    cmd += ['-c:a', 'copy']
                else:
                    cmd += ['-an']
                cmd += ['-movflags', '+faststart', tmpname]

                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(proc.communicate(), timeout=120.0)
                except asyncio.TimeoutError:
                    try:
                        proc.kill()
                    except ProcessLookupError:
                        pass
                    raise web.HTTPGatewayTimeout()

                if proc.returncode != 0:
                    raise web.HTTPInternalServerError()

                mp4_bytes = Path(tmpname).read_bytes()
            finally:
                try:
                    os.unlink(tmpname)
                except OSError:
                    pass

            if not mp4_bytes:
                raise web.HTTPInternalServerError()

            # Store in LRU cache, evict oldest if over limit
            _mp4_cache[cache_key] = mp4_bytes
            _mp4_cache.move_to_end(cache_key)
            while len(_mp4_cache) > MAX_MP4_CACHE:
                evicted_key, _ = _mp4_cache.popitem(last=False)
                _mp4_locks.pop(evicted_key, None)

    total = len(mp4_bytes)
    base_headers: dict[str, str] = {
        'Content-Type': 'video/mp4',
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'public, max-age=3600',
        'X-Content-Type-Options': 'nosniff',
    }

    # Handle Range requests — needed for iOS probe (bytes=0-1) and for scrubbing
    range_val = request.headers.get('Range', '')
    m = re.match(r'bytes=(\d+)-(\d*)', range_val)
    if m:
        start = int(m.group(1))
        end   = int(m.group(2)) if m.group(2) else total - 1
        end   = min(end, total - 1)
        if start > end or start >= total:
            return web.Response(
                status=416,
                headers={**base_headers, 'Content-Range': f'bytes */{total}'},
            )
        length = end - start + 1
        return web.Response(
            body=mp4_bytes[start:end + 1],
            status=206,
            headers={
                **base_headers,
                'Content-Length': str(length),
                'Content-Range': f'bytes {start}-{end}/{total}',
            },
        )

    return web.Response(
        body=mp4_bytes,
        headers={**base_headers, 'Content-Length': str(total)},
    )


# ---------------------------------------------------------------------------
# Audio endpoint — extract rawAudioData PCM from rlog.zst and serve as WAV
# ---------------------------------------------------------------------------

def _rlog_path(seg_dir: Path) -> Path | None:
    """Return path to rlog file in segment directory, or None."""
    for name in ('rlog.zst', 'rlog'):
        p = seg_dir / name
        if p.exists():
            return p
    return None


def _extract_audio_wav(rlog_path: Path) -> bytes | None:
    """Read rlog(.zst), extract rawAudioData messages, return WAV bytes or None."""
    try:
        import zstandard as zstd  # available in openpilot venv
        import capnp  # noqa: F401
        from cereal import log as capnp_log
    except ImportError:
        return None

    try:
        raw = rlog_path.read_bytes()
        # Decompress if zstd
        if raw[:4] == b'\x28\xB5\x2F\xFD':
            dctx = zstd.ZstdDecompressor()
            raw = dctx.decompress(raw, max_output_size=256 * 1024 * 1024)

        events = capnp_log.Event.read_multiple_bytes(raw)
        chunks: list[bytes] = []
        sample_rate = 16000
        for e in events:
            try:
                if e.which() == 'rawAudioData':
                    rad = e.rawAudioData
                    chunks.append(bytes(rad.data))
                    sample_rate = rad.sampleRate
            except Exception:
                continue
    except Exception:
        return None

    if not chunks:
        return None

    pcm = b''.join(chunks)
    buf = io.BytesIO()
    with wave.open(buf, 'wb') as w:
        w.setnchannels(1)
        w.setsampwidth(2)        # int16
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _scan_for_bookmarks(seg_dir: Path) -> list[float]:
    """Return video timestamps (seconds from segment start) of userBookmark events."""
    rlog = _rlog_path(seg_dir)
    if not rlog:
        return []
    try:
        import zstandard as zstd
        from cereal import log as capnp_log

        raw = rlog.read_bytes()
        if raw[:4] == b'\x28\xB5\x2F\xFD':
            dctx = zstd.ZstdDecompressor()
            raw = dctx.decompress(raw, max_output_size=256 * 1024 * 1024)

        events = capnp_log.Event.read_multiple_bytes(raw)
        first_mono: int | None = None
        bookmarks: list[float] = []
        for e in events:
            try:
                if first_mono is None:
                    first_mono = e.logMonoTime
                if e.which() == 'userBookmark':
                    bookmarks.append(round((e.logMonoTime - first_mono) / 1e9, 2))
            except Exception:
                continue
        return bookmarks
    except Exception:
        return []


async def handle_audio(request: web.Request) -> web.Response:
    """Stream WAV audio extracted from the segment's rlog.zst."""
    route_id = request.match_info['route_id']
    try:
        segment = int(request.match_info['segment'])
    except (ValueError, KeyError):
        raise web.HTTPBadRequest()
    if not ROUTE_ID_PATTERN.match(route_id):
        raise web.HTTPBadRequest()
    if not (0 <= segment <= 99999):
        raise web.HTTPBadRequest()

    realdata_resolved = REALDATA.resolve()
    seg_dir = (REALDATA / f"{route_id}--{segment}").resolve()
    if not str(seg_dir).startswith(str(realdata_resolved) + os.sep):
        raise web.HTTPForbidden()
    if not seg_dir.is_dir():
        raise web.HTTPNotFound()

    rlog = _rlog_path(seg_dir)
    if not rlog:
        raise web.HTTPNotFound()

    loop = asyncio.get_event_loop()
    wav_bytes = await loop.run_in_executor(None, _extract_audio_wav, rlog)
    if not wav_bytes:
        raise web.HTTPNotFound()

    return web.Response(
        body=wav_bytes,
        content_type='audio/wav',
        headers={'Cache-Control': 'no-cache'},
    )


async def handle_api_bookmarks(request: web.Request) -> web.Response:
    """Return {segment_num: [timestamp_seconds, ...]} for segments with bookmarks."""
    route_id = request.match_info['route_id']
    if not ROUTE_ID_PATTERN.match(route_id):
        raise web.HTTPBadRequest()

    loop = asyncio.get_event_loop()
    routes = await loop.run_in_executor(None, _scan_routes)
    route = next((r for r in routes if r['id'] == route_id), None)
    if not route:
        raise web.HTTPNotFound()

    realdata_resolved = REALDATA.resolve()
    result: dict[int, list[float]] = {}
    for seg_num in route['segments']:
        seg_dir = (REALDATA / f"{route_id}--{seg_num}").resolve()
        if not str(seg_dir).startswith(str(realdata_resolved) + os.sep):
            continue
        bookmarks = await loop.run_in_executor(None, _scan_for_bookmarks, seg_dir)
        if bookmarks:
            result[seg_num] = bookmarks

    return web.json_response(result)


# ---------------------------------------------------------------------------
# Embedded single-page UI  (mobile-first, Tailwind CDN)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Dashcam</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
/* ── Reset & base ── */
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0;}
html,body{height:100%;background:#09090b;color:#f4f4f5;font-family:system-ui,-apple-system,sans-serif;overflow:hidden;}

/* ── Layout shell ── */
#app{display:flex;flex-direction:column;height:100dvh;}
#header{display:flex;align-items:center;gap:10px;padding:0 16px;height:52px;background:#09090b;border-bottom:1px solid #27272a;flex-shrink:0;}

/* Mobile: column (player on top, list below) */
#body{flex:1;display:flex;flex-direction:column;min-height:0;overflow:hidden;}
#player-area{display:flex;flex-direction:column;flex:0 0 55%;min-height:0;}
#list-drawer{flex:1;display:flex;flex-direction:column;border-top:1px solid #27272a;min-height:0;}

/* Desktop: row */
@media(min-width:768px){
  #body{flex-direction:row;}
  #player-area{flex:1;min-width:0;}
  #list-drawer{flex:0 0 300px;border-top:none;border-left:1px solid #27272a;}
}

/* ── Video ── */
#video-wrap{flex:1;display:flex;align-items:center;justify-content:center;background:#000;min-height:0;position:relative;}
video{width:100%;height:100%;object-fit:contain;display:block;background:#000;}
#video-overlay{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:#71717a;pointer-events:none;font-size:14px;}
#video-overlay.hidden{display:none;}

/* ── Controls strip (below video) ── */
#controls{flex-shrink:0;background:#18181b;border-top:1px solid #27272a;padding:10px 14px;display:flex;flex-direction:column;gap:8px;}
#ctrl-row1{display:flex;align-items:center;gap:8px;flex-wrap:wrap;}
#seg-scroll{display:flex;gap:5px;overflow-x:auto;padding:2px 0;flex:1;scrollbar-width:none;}
#seg-scroll::-webkit-scrollbar{display:none;}
.cam-btn{padding:5px 12px;border-radius:6px;border:1px solid #3f3f46;background:#27272a;color:#a1a1aa;font-size:12px;cursor:pointer;white-space:nowrap;transition:all 0.1s;touch-action:manipulation;}
.cam-btn.active,.cam-btn:active{background:#22c55e;border-color:#22c55e;color:#000;font-weight:600;}
.nav-btn{width:34px;height:34px;border-radius:8px;border:1px solid #3f3f46;background:#27272a;color:#a1a1aa;font-size:16px;cursor:pointer;display:flex;align-items:center;justify-content:center;flex-shrink:0;touch-action:manipulation;transition:all 0.1s;}
.nav-btn:active{background:#3f3f46;color:#fff;}
.nav-btn:disabled{opacity:0.3;cursor:default;}
.seg-chip{flex-shrink:0;padding:4px 10px;border-radius:6px;border:1px solid #3f3f46;background:#27272a;color:#a1a1aa;font-size:11px;font-family:ui-monospace,monospace;cursor:pointer;white-space:nowrap;touch-action:manipulation;transition:all 0.1s;}
.seg-chip.active{background:#22c55e;border-color:#22c55e;color:#000;font-weight:700;}
.seg-chip.bookmarked{border-color:#eab308;color:#ca8a04;}
.seg-chip.bookmarked.active{background:#eab308;border-color:#eab308;color:#000;}
#bookmark-row{display:flex;flex-wrap:wrap;gap:5px;align-items:center;}
.bm-btn{padding:3px 8px;border-radius:5px;border:1px solid #eab308;background:transparent;color:#eab308;font-size:11px;font-family:ui-monospace,monospace;cursor:pointer;touch-action:manipulation;}
.bm-btn:active{background:#eab308;color:#000;}
#route-meta{display:flex;justify-content:space-between;align-items:center;gap:8px;}
#route-title{font-size:13px;font-weight:500;line-height:1.3;}
#route-sub{font-size:11px;color:#71717a;font-family:ui-monospace,monospace;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}

/* ── Route list ── */
#list-header{flex-shrink:0;padding:10px 12px;border-bottom:1px solid #27272a;display:flex;flex-direction:column;gap:8px;}
#search-input{width:100%;background:#27272a;border:1px solid #3f3f46;border-radius:8px;padding:8px 12px;color:#f4f4f5;font-size:14px;outline:none;transition:border-color 0.15s;}
#search-input:focus{border-color:#71717a;}
#route-count-label{font-size:11px;color:#52525b;}
#route-list{flex:1;overflow-y:auto;padding:8px;}
.route-card{border-radius:10px;border:1px solid #27272a;background:#18181b;padding:12px;margin-bottom:6px;cursor:pointer;touch-action:manipulation;transition:border-color 0.12s,background 0.12s;-webkit-tap-highlight-color:transparent;}
.route-card:active{background:#27272a;}
.route-card.selected{border-color:#22c55e;background:#052e16;}
.route-card .rc-date{font-size:14px;font-weight:500;line-height:1.3;}
.route-card .rc-time{font-size:12px;color:#71717a;margin-top:1px;}
.route-card .rc-id{font-size:10px;color:#3f3f46;font-family:ui-monospace,monospace;margin-top:4px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.route-card .rc-badge{font-size:11px;color:#a1a1aa;white-space:nowrap;}

/* ── Misc ── */
::-webkit-scrollbar{width:3px;height:3px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:#3f3f46;border-radius:2px;}
#refresh-btn{margin-left:auto;padding:6px 12px;border-radius:8px;border:1px solid #3f3f46;background:transparent;color:#a1a1aa;font-size:12px;cursor:pointer;touch-action:manipulation;transition:all 0.1s;}
#refresh-btn:active{background:#27272a;color:#fff;}
</style>
</head>
<body>
<div id="app">

  <!-- Header -->
  <div id="header">
    <span style="color:#22c55e;font-size:10px;">●</span>
    <span style="font-size:16px;font-weight:600;letter-spacing:-0.3px;">Dashcam</span>
    <button id="refresh-btn" onclick="refreshRoutes()">↺ Refresh</button>
  </div>

  <!-- Body -->
  <div id="body">

    <!-- Player side -->
    <div id="player-area">
      <!-- Video -->
      <div id="video-wrap">
        <video id="video" playsinline controls preload="auto" onended="onVideoEnded()"></video>
        <audio id="audio-sync" preload="auto" style="display:none;"></audio>
        <div id="video-overlay">
          <svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M15 10l4.553-2.069A1 1 0 0121 8.882v6.236a1 1 0 01-1.447.894L15 14M4 8h8a2 2 0 012 2v4a2 2 0 01-2 2H4a2 2 0 01-2-2v-4a2 2 0 012-2z"/>
          </svg>
          <span>Select a route</span>
        </div>
      </div>

      <!-- Controls (hidden until route selected) -->
      <div id="controls" style="display:none;">
        <div id="route-meta">
          <div style="min-width:0;">
            <div id="route-title"></div>
            <div id="route-sub"></div>
          </div>
        </div>
        <!-- Camera selector + nav -->
        <div id="ctrl-row1">
          <button class="nav-btn" id="prev-btn" onclick="stepSegment(-1)" title="Previous segment">‹</button>
          <div id="seg-scroll"></div>
          <button class="nav-btn" id="next-btn" onclick="stepSegment(1)" title="Next segment">›</button>
        </div>
        <!-- Camera tabs -->
        <div style="display:flex;gap:5px;flex-wrap:wrap;" id="cam-tabs"></div>
        <!-- Bookmark jump buttons (visible only when current segment has bookmarks) -->
        <div id="bookmark-row" style="display:none;"></div>
      </div>
    </div>

    <!-- Route list drawer -->
    <div id="list-drawer">
      <div id="list-header">
        <input id="search-input" type="search" placeholder="Search routes…" oninput="onSearch(this.value)">
        <span id="route-count-label"></span>
      </div>
      <div id="route-list">
        <div style="text-align:center;padding:48px 0;color:#52525b;font-size:13px;">Loading…</div>
      </div>
    </div>

  </div>
</div>

<script>
const CAMERAS = [
  {key:'q', label:'Road (360p)', hasAudio: true},
  {key:'f', label:'Front'},
  {key:'d', label:'Driver'},
  {key:'e', label:'Wide'},
];

// ── Timezone helpers (browser-local timezone) ──────────────────────────────
function formatDate(mtime) {
  const dt = new Date(mtime * 1000);
  return dt.toLocaleDateString('en-US', {month:'short', day:'numeric', year:'numeric'});
}
function formatTime(mtime) {
  const dt = new Date(mtime * 1000);
  return dt.toLocaleTimeString('en-US', {hour:'numeric', minute:'2-digit'});
}

let allRoutes = [];
let currentRoute = null;
let currentSegIdx = 0;
let currentCam = 'q';
let routeBookmarks = {};

// ── Boot ─────────────────────────────────────────────────────────────────

async function loadRoutes() {
  try {
    const r = await fetch('/api/routes');
    if (!r.ok) throw new Error(r.status);
    allRoutes = await r.json();
    renderRouteList(allRoutes);
    updateCount(allRoutes.length, allRoutes.length);
  } catch(e) {
    document.getElementById('route-list').innerHTML =
      '<div style="text-align:center;padding:48px 16px;color:#ef4444;font-size:13px;">Failed to load routes.<br>Is the device reachable?</div>';
  }
}

function refreshRoutes() {
  document.getElementById('route-list').innerHTML =
    '<div style="text-align:center;padding:48px 0;color:#52525b;font-size:13px;">Loading…</div>';
  loadRoutes();
}

// ── Route list ────────────────────────────────────────────────────────────

function renderRouteList(routes) {
  const el = document.getElementById('route-list');
  if (!routes.length) {
    el.innerHTML = '<div style="text-align:center;padding:48px 0;color:#52525b;font-size:13px;">No routes found.</div>';
    return;
  }
  el.innerHTML = routes.map(r => {
    const sel = currentRoute && currentRoute.id === r.id ? ' selected' : '';
    const d = formatDate(r.mtime);
    const t = formatTime(r.mtime);
    return `<div class="route-card${sel}" onclick="selectRoute('${r.id}')">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px;">
        <div style="min-width:0;">
          <div class="rc-date">${d}</div>
          <div class="rc-time">${t}</div>
          <div class="rc-id">${r.id}</div>
        </div>
        <div class="rc-badge" style="flex-shrink:0;text-align:right;">
          <div>${r.seg_count} seg</div>
          <div style="color:#52525b;">~${r.seg_count}m</div>
        </div>
      </div>
    </div>`;
  }).join('');
}

function updateCount(shown, total) {
  document.getElementById('route-count-label').textContent =
    shown === total ? `${total} route${total !== 1 ? 's' : ''}`
                    : `${shown} / ${total} routes`;
}

function onSearch(q) {
  q = q.toLowerCase().trim();
  const filtered = q
    ? allRoutes.filter(r => r.id.includes(q) || formatDate(r.mtime).toLowerCase().includes(q) || formatTime(r.mtime).toLowerCase().includes(q))
    : allRoutes;
  renderRouteList(filtered);
  updateCount(filtered.length, allRoutes.length);
}

// ── Route select & playback ───────────────────────────────────────────────

async function selectRoute(routeId) {
  let route;
  try {
    const r = await fetch(`/api/route/${routeId}`);
    if (!r.ok) throw new Error(r.status);
    route = await r.json();
  } catch { return; }

  currentRoute = route;
  currentSegIdx = 0;
  currentCam = 'q';

  // hide empty-state overlay
  document.getElementById('video-overlay').classList.add('hidden');

  // show controls
  document.getElementById('controls').style.display = 'flex';
  document.getElementById('controls').style.flexDirection = 'column';

  // metadata
  document.getElementById('route-title').textContent = `${formatDate(route.mtime)} · ${formatTime(route.mtime)}`;
  document.getElementById('route-sub').textContent = route.id;

  // camera tabs
  const tabs = document.getElementById('cam-tabs');
  tabs.innerHTML = CAMERAS.map(c =>
    `<button class="cam-btn${c.key===currentCam?' active':''}" id="camtab-${c.key}" onclick="switchCam('${c.key}')">${c.label}</button>`
  ).join('');

  // re-render route list (update selected state)
  const q = document.getElementById('search-input').value.toLowerCase().trim();
  const filtered = q ? allRoutes.filter(r => r.id.includes(q) || formatDate(r.mtime).toLowerCase().includes(q) || formatTime(r.mtime).toLowerCase().includes(q)) : allRoutes;
  renderRouteList(filtered);

  // Reset bookmarks then fetch in background — chips update when data arrives
  routeBookmarks = {};
  renderSegStrip();
  playSegment(0);

  fetch(`/api/bookmarks/${routeId}`)
    .then(r => r.ok ? r.json() : {})
    .then(bm => {
      routeBookmarks = bm;
      renderSegStrip();
      updateBookmarkRow(currentRoute?.segments[currentSegIdx]);
    })
    .catch(() => {});
}

function renderSegStrip() {
  const strip = document.getElementById('seg-scroll');
  strip.innerHTML = (currentRoute?.segments ?? []).map((seg, idx) => {
    const hasBm = (routeBookmarks[seg] || []).length > 0;
    const cls = 'seg-chip' + (idx===currentSegIdx?' active':'') + (hasBm?' bookmarked':'');
    const label = hasBm ? `★ ${String(seg).padStart(2,'0')}` : String(seg).padStart(2,'0');
    return `<button class="${cls}" id="sc-${seg}" onclick="clickSeg(${idx})">${label}</button>`;
  }).join('');

  // nav-btn state
  document.getElementById('prev-btn').disabled = currentSegIdx <= 0;
  document.getElementById('next-btn').disabled = currentSegIdx >= currentRoute.segments.length - 1;
}

function clickSeg(idx) {
  currentSegIdx = idx;
  renderSegStrip();
  playSegment(idx);
}

function stepSegment(delta) {
  const next = currentSegIdx + delta;
  if (!currentRoute || next < 0 || next >= currentRoute.segments.length) return;
  clickSeg(next);
}

function switchCam(key) {
  currentCam = key;
  document.querySelectorAll('.cam-btn').forEach(b => b.classList.remove('active'));
  const btn = document.getElementById(`camtab-${key}`);
  if (btn) btn.classList.add('active');
  playSegment(currentSegIdx);
}

function playSegment(idx) {
  currentSegIdx = idx;
  if (!currentRoute) return;
  const seg = currentRoute.segments[idx];

  // Update chip highlight
  document.querySelectorAll('.seg-chip').forEach(c => c.classList.remove('active'));
  const chip = document.getElementById(`sc-${seg}`);
  if (chip) {
    chip.classList.add('active');
    chip.scrollIntoView({behavior:'smooth', block:'nearest', inline:'center'});
  }

  // Update nav buttons
  document.getElementById('prev-btn').disabled = idx <= 0;
  document.getElementById('next-btn').disabled = idx >= currentRoute.segments.length - 1;

  // Stop external audio from the previous segment
  const audio = document.getElementById('audio-sync');
  audio.pause();
  audio.src = '';
  audio.removeAttribute('src');

  // Load and play video — /stream endpoint pipes ffmpeg→fMP4
  const video = document.getElementById('video');
  video.src = `/stream/${currentRoute.id}/${seg}/${currentCam}`;
  video.load();

  // For f/d/e cameras: load separate audio from rlog if available.
  // (q camera has audio muxed directly into the MP4 stream.)
  const camInfo = CAMERAS.find(c => c.key === currentCam);
  if (!camInfo?.hasAudio) {
    audio.src = `/audio/${currentRoute.id}/${seg}`;
    audio.load();  // start buffering immediately so canplay fires before video plays
  }

  video.play().catch(() => {});
  updateBookmarkRow(seg);
}

function onVideoEnded() {
  if (currentRoute && currentSegIdx < currentRoute.segments.length - 1) {
    stepSegment(1);
  }
}

// ── Bookmarks ──────────────────────────────────────────────────────────────
function formatSeconds(secs) {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${String(s).padStart(2,'0')}`;
}

function updateBookmarkRow(seg) {
  const bm = (seg !== undefined && seg !== null) ? (routeBookmarks[seg] || []) : [];
  const row = document.getElementById('bookmark-row');
  if (!bm.length) { row.style.display = 'none'; return; }
  row.style.display = 'flex';
  row.innerHTML = '<span style="font-size:11px;color:#eab308;flex-shrink:0;">★ Jump:</span>'
    + bm.map(t => `<button class="bm-btn" onclick="document.getElementById('video').currentTime=${t}">${formatSeconds(t)}</button>`).join('');
}

// ── Audio sync ─────────────────────────────────────────────────────────────
(function setupAudioSync() {
  const video = document.getElementById('video');
  const audio = document.getElementById('audio-sync');

  function hasAudioSrc() {
    return audio.src && audio.src !== window.location.href;
  }

  function syncAudio() {
    // Don't touch audio until it has data and isn't mid-seek — that causes glitches
    if (!hasAudioSrc() || audio.readyState < 2 || audio.seeking) return;
    const diff = Math.abs(audio.currentTime - video.currentTime);
    if (diff > 0.5) audio.currentTime = video.currentTime;
  }

  // If audio finishes loading after video already started playing, start it now
  audio.addEventListener('canplay', () => {
    if (!video.paused && hasAudioSrc()) {
      audio.currentTime = video.currentTime;
      audio.play().catch(() => {});
    }
  });

  video.addEventListener('play', () => {
    if (!hasAudioSrc()) return;
    if (audio.readyState >= 2) {
      audio.currentTime = video.currentTime;
      audio.play().catch(() => {});
    }
    // else: canplay listener above will fire when audio is ready
  });
  video.addEventListener('pause', () => { audio.pause(); });
  video.addEventListener('seeked', () => {
    if (!hasAudioSrc() || audio.readyState < 2) return;
    audio.currentTime = video.currentTime;
  });
  video.addEventListener('timeupdate', syncAudio);
})();

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
    app.router.add_get('/',                                  handle_index)
    app.router.add_get('/api/routes',                        handle_api_routes)
    app.router.add_get('/api/route/{route_id}',              handle_api_route)
    app.router.add_get('/stream/{route_id}/{segment}',       handle_stream)
    app.router.add_get('/stream/{route_id}/{segment}/{camera}', handle_stream)
    app.router.add_get('/audio/{route_id}/{segment}',         handle_audio)
    app.router.add_get('/api/bookmarks/{route_id}',           handle_api_bookmarks)

    web.run_app(app, host='0.0.0.0', port=PORT, access_log=None)


if __name__ == '__main__':
    main()
