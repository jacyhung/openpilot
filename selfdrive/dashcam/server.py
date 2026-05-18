"""Dashcam video browser — aiohttp server on port 8082.

Routes are read from /data/media/0/realdata.  Each directory whose name
matches  {bootcount}--{routehash}--{segnum}  is a one-minute segment.
The server groups segments into routes and exposes them via a JSON API.

Video is remuxed by ffmpeg (HEVC/H.264 → regular MP4) and served with full
byte-range support so browsers and iOS can scrub freely.
"""

import asyncio
import base64
import collections
import dataclasses
import json
import os
import re
import shutil
import ssl
import subprocess
import tempfile
import time
from pathlib import Path

from aiohttp import ClientSession, ClientTimeout, web

# Hardcoded ffmpeg paths for the comma device (managed processes have no PATH).
_FFMPEG_PATHS = [
    # Real ffmpeg binary (not the pip-package Python wrapper at /usr/local/venv/bin/ffmpeg)
    "/usr/local/venv/lib/python3.12/site-packages/ffmpeg/install/bin/ffmpeg",
    "/usr/bin/ffmpeg",
    shutil.which("ffmpeg"),  # PATH fallback
]
_FFMPEG = next((p for p in _FFMPEG_PATHS if p and os.path.exists(p)), None)
_FFPROBE = None
if _FFMPEG:
    d = os.path.dirname(_FFMPEG)
    _FFPROBE = os.path.join(d, "ffprobe")
if not _FFMPEG or not _FFPROBE or not os.path.exists(_FFPROBE):
    raise RuntimeError("ffmpeg/ffprobe not found on any known path")

REALDATA = Path(os.environ.get("DASHCAM_DATA_DIR", "/data/media/0/realdata"))
PORT = int(os.environ.get("DASHCAM_PORT", "8082"))
DASHCAM_USER = os.environ.get("DASHCAM_USER", "comma")
DASHCAM_PASS = os.environ.get("DASHCAM_PASS", "comma")

# WebRTC proxy settings — webrtcd runs as a managed process on localhost:5001
WEBRTCD_HOST = os.environ.get("WEBRTCD_HOST", "localhost")
WEBRTCD_PORT = int(os.environ.get("WEBRTCD_PORT", "5001"))

# Self-signed certificate cache for optional HTTPS (WebRTC requires secure context)
_SSL_CERT_DIR = Path("/data/tmp/dashcam_ssl")
_SSL_CERT_PATH = _SSL_CERT_DIR / "cert.pem"
_SSL_KEY_PATH = _SSL_CERT_DIR / "key.pem"


def _ensure_ssl_cert() -> ssl.SSLContext | None:
    """Create or reuse a self-signed certificate and return an SSLContext."""
    try:
        if not (_SSL_CERT_PATH.exists() and _SSL_KEY_PATH.exists()):
            _SSL_CERT_DIR.mkdir(parents=True, exist_ok=True)
            proc = subprocess.run(
                [
                    "openssl", "req", "-x509", "-newkey", "rsa:2048",
                    "-nodes", "-out", str(_SSL_CERT_PATH),
                    "-keyout", str(_SSL_KEY_PATH),
                    "-days", "365",
                    "-subj", "/C=US/ST=California/O=commaai/CN=comma-device",
                ],
                capture_output=True,
            )
            proc.check_returncode()
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(_SSL_CERT_PATH), str(_SSL_KEY_PATH))
        return ctx
    except Exception:
        return None

# LRU cache for remuxed MP4 bytes — avoids re-running ffmpeg on every Range
# sub-request. Keyed on (route_id, segment, camera). 10 entries ≈ 250-750 MB max.
MAX_MP4_CACHE = 10
_mp4_cache: collections.OrderedDict[tuple[str, int, str], bytes] = collections.OrderedDict()
_mp4_locks: dict[tuple[str, int, str], asyncio.Lock] = {}
_remux_sem = asyncio.Semaphore(2)

# Persistent disk cache for remuxed MP4s — survives restarts and avoids
# repeated ffmpeg CPU/battery cost. Kept in /data/tmp so it can be cleared
# by a reboot or standard tmp cleanup.
DISK_CACHE_DIR = Path(os.environ.get("DASHCAM_DISK_CACHE", "/data/tmp/dashcam_cache"))
DISK_CACHE_MAX_AGE = 86400 * 7  # keep cached files for 7 days

def _disk_cache_path(route_id: str, segment: int, camera: str) -> Path:
    safe = re.sub(r"[^0-9a-zA-Z_-]", "_", f"{route_id}--{segment}--{camera}")
    return DISK_CACHE_DIR / f"{safe}.mp4"

def _read_disk_cache_sync(route_id: str, segment: int, camera: str) -> bytes | None:
    path = _disk_cache_path(route_id, segment, camera)
    try:
        st = path.stat()
        if time.time() - st.st_mtime < DISK_CACHE_MAX_AGE:
            return path.read_bytes()
    except (OSError, ValueError):
        pass
    return None

async def _read_disk_cache(route_id: str, segment: int, camera: str) -> bytes | None:
    return await asyncio.to_thread(_read_disk_cache_sync, route_id, segment, camera)

def _write_disk_cache_sync(route_id: str, segment: int, camera: str, data: bytes) -> None:
    path = _disk_cache_path(route_id, segment, camera)
    try:
        DISK_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix('.tmp')
        tmp.write_bytes(data)
        os.replace(str(tmp), str(path))
    except OSError:
        pass

async def _write_disk_cache(route_id: str, segment: int, camera: str, data: bytes) -> None:
    await asyncio.to_thread(_write_disk_cache_sync, route_id, segment, camera, data)

BOOKMARK_CACHE_TTL = 300.0
_bookmark_cache: dict[str, tuple[float, dict[int, list[float]]]] = {}
_bookmark_locks: dict[str, asyncio.Lock] = {}
_bookmark_scan_sem = asyncio.Semaphore(2)

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


def _seg_mtime(seg_dir: Path) -> float:
    """Earliest file mtime inside *seg_dir* (fallback to directory mtime if empty)."""
    earliest: float | None = None
    try:
        for child in os.scandir(seg_dir):
            if child.is_file():
                st = child.stat()
                if earliest is None or st.st_mtime < earliest:
                    earliest = st.st_mtime
    except OSError:
        pass
    return earliest if earliest is not None else seg_dir.stat().st_mtime


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
        mtime = _seg_mtime(Path(entry.path))
        if route_id not in routes:
            routes[route_id] = {'id': route_id, 'mtime': mtime, 'segments': []}
        elif mtime < routes[route_id]['mtime']:
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


async def _has_valid_audio(audio_path: Path) -> bool:
    """Check whether *audio_path* contains at least one usable audio stream."""
    import json
    proc = await asyncio.create_subprocess_exec(
        _FFPROBE, '-hide_banner', '-loglevel', 'error',
        '-select_streams', 'a', '-show_entries',
        'stream=codec_type,sample_rate,channels',
        '-of', 'json', str(audio_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        return False
    if proc.returncode != 0:
        return False
    try:
        info = json.loads(out)
        for st in info.get('streams', []):
            sr = st.get('sample_rate')
            ch = st.get('channels')
            if sr and int(sr) > 0 and ch and int(ch) > 0:
                return True
    except (json.JSONDecodeError, ValueError, TypeError):
        pass
    return False


async def _remux(
    video_path: Path, audio_path: Path | None, fmt: str | None,
) -> bytes:
    """Run ffmpeg to produce a fast-start MP4.

    For f/d/e cameras with a separate audio source we probe the audio first.
    If it's corrupt (segment-0 qcamera.ts often has a 0-channel AAC stream)
    we skip audio entirely rather than try-and-fallback.
    """
    try_audio = audio_path is not None and await _has_valid_audio(audio_path)

    cmd = [_FFMPEG, '-hide_banner', '-loglevel', 'error', '-y']
    if fmt:
        cmd += ['-f', fmt]
    cmd += ['-i', str(video_path)]
    if try_audio:
        cmd += ['-i', str(audio_path)]
        cmd += ['-map', '0:v:0', '-map', '1:a:0?', '-c:v', 'copy', '-c:a', 'copy']
    else:
        cmd += ['-c:v', 'copy']
        # qcamera has audio muxed inside the same file
        if audio_path is None:
            cmd += ['-c:a', 'copy']
    if fmt == 'hevc':
        cmd += ['-tag:v', 'hvc1']

    tmp_dir = Path(os.environ.get("DASHCAM_TMP", "/data/tmp"))
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmpname = tempfile.mkstemp(suffix='.mp4', dir=str(tmp_dir))
    os.close(tmp_fd)
    try:
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
        return Path(tmpname).read_bytes()
    finally:
        try:
            os.unlink(tmpname)
        except OSError:
            pass


async def handle_stream(request: web.Request) -> web.Response:
  """Build a regular MP4 via ffmpeg and serve it with full Range support.

  For qcamera, the output is video+embedded audio from qcamera.ts.
  For f/d/e cameras, the output is that camera's video plus the audio track
  from qcamera.ts. This keeps playback in a single media element so browser
  controls handle mute, volume, buffering, and A/V sync natively.
  """
  route_id = request.match_info['route_id']
  camera   = request.match_info.get('camera', 'q')
  try:
    segment = int(request.match_info['segment'])
  except (ValueError, KeyError):
    raise web.HTTPBadRequest()

  video_path = _validate_video_path(route_id, segment, camera)
  audio_path = _validate_video_path(route_id, segment, 'q') if camera != 'q' else None
  _, fmt = CAMERAS[camera]

  cache_key = (route_id, segment, camera)

  # Fast path 1: already in memory cache — no lock needed.
  if cache_key in _mp4_cache:
    _mp4_cache.move_to_end(cache_key)
    mp4_bytes = _mp4_cache[cache_key]
  else:
    # Fast path 2: disk cache — also no lock needed.  Multiple concurrent
    # reads are fine; they all just populate the memory cache redundantly.
    mp4_bytes = await _read_disk_cache(route_id, segment, camera)
    if mp4_bytes is not None:
      _mp4_cache[cache_key] = mp4_bytes
      _mp4_cache.move_to_end(cache_key)
    else:
      # Slow path: need a per-key lock so only one request remuxes.
      if cache_key not in _mp4_locks:
        _mp4_locks[cache_key] = asyncio.Lock()
      lock = _mp4_locks[cache_key]

      cancelled = False
      async with lock:
        # Re-check memory cache inside lock (another request may have populated it)
        if cache_key in _mp4_cache:
          _mp4_cache.move_to_end(cache_key)
          mp4_bytes = _mp4_cache[cache_key]
        else:
          # Re-check disk cache inside lock (another request may have written it)
          mp4_bytes = await _read_disk_cache(route_id, segment, camera)
          if mp4_bytes is not None:
            _mp4_cache[cache_key] = mp4_bytes
            _mp4_cache.move_to_end(cache_key)
          else:
            # Run ffmpeg once; cache the result.
            # Semaphore(2) ensures at most 2 concurrent remux processes.
            async with _remux_sem:
              # Re-check cache again after acquiring the remux semaphore
              if cache_key in _mp4_cache:
                _mp4_cache.move_to_end(cache_key)
                mp4_bytes = _mp4_cache[cache_key]
              else:
                if request.transport is None or request.transport.is_closing():
                  cancelled = True
                  mp4_bytes = b''
                else:
                  mp4_bytes = await _remux(video_path, audio_path, fmt)

                  if not mp4_bytes:
                    raise web.HTTPInternalServerError()

                  # Store in LRU cache, evict oldest if over limit
                  _mp4_cache[cache_key] = mp4_bytes
                  _mp4_cache.move_to_end(cache_key)
                  while len(_mp4_cache) > MAX_MP4_CACHE:
                    evicted_key, _ = _mp4_cache.popitem(last=False)
                    _mp4_locks.pop(evicted_key, None)

      if cancelled:
        return web.Response(status=499)

      # Persist to disk in the background so the response returns immediately.
      # The memory cache is already populated, so concurrent requests will
      # hit that fast path.  The atomic temp-file + rename guarantees that
      # a future disk read never sees a partial file.
      asyncio.create_task(_write_disk_cache(route_id, segment, camera, mp4_bytes))

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
# Helpers for rlog-based bookmark scanning
# ---------------------------------------------------------------------------

def _rlog_path(seg_dir: Path) -> Path | None:
    """Return path to rlog file in segment directory, or None."""
    for name in ('rlog.zst', 'rlog'):
        p = seg_dir / name
        if p.exists():
            return p
    return None


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


async def handle_api_bookmarks(request: web.Request) -> web.Response:
    """Return {segment_num: [timestamp_seconds, ...]} for segments with bookmarks."""
    route_id = request.match_info['route_id']
    if not ROUTE_ID_PATTERN.match(route_id):
        raise web.HTTPBadRequest()

    now = time.monotonic()
    cached = _bookmark_cache.get(route_id)
    if cached and now - cached[0] < BOOKMARK_CACHE_TTL:
        return web.json_response(cached[1], headers={'Cache-Control': 'public, max-age=300'})

    if route_id not in _bookmark_locks:
        _bookmark_locks[route_id] = asyncio.Lock()

    loop = asyncio.get_event_loop()
    async with _bookmark_locks[route_id]:
        now = time.monotonic()
        cached = _bookmark_cache.get(route_id)
        if cached and now - cached[0] < BOOKMARK_CACHE_TTL:
            return web.json_response(cached[1], headers={'Cache-Control': 'public, max-age=300'})

        routes = await loop.run_in_executor(None, _scan_routes)
        route = next((r for r in routes if r['id'] == route_id), None)
        if not route:
            raise web.HTTPNotFound()

        realdata_resolved = REALDATA.resolve()

        async def scan_segment(seg_num: int) -> tuple[int, list[float]]:
            seg_dir = (REALDATA / f"{route_id}--{seg_num}").resolve()
            if not str(seg_dir).startswith(str(realdata_resolved) + os.sep):
                return seg_num, []
            async with _bookmark_scan_sem:
                bookmarks = await loop.run_in_executor(None, _scan_for_bookmarks, seg_dir)
            return seg_num, bookmarks

        results = await asyncio.gather(*(scan_segment(seg_num) for seg_num in route['segments']))
        result = {seg: bm for seg, bm in results if bm}
        _bookmark_cache[route_id] = (time.monotonic(), result)
        return web.json_response(result, headers={'Cache-Control': 'public, max-age=300'})


# ---------------------------------------------------------------------------
# Embedded single-page UI  (mobile-first, Tailwind CDN)
# ---------------------------------------------------------------------------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>Dashcam</title>
<!-- No external CDN deps — all styles are custom and inlined -->
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

@media(max-width:767px){
  #app{height:100dvh;min-height:0;}
  #body{overflow:hidden;}
  #player-area{flex:0 0 auto;min-height:auto;}
  #video-wrap{flex:0 0 auto;aspect-ratio:16/9;width:100%;max-height:42dvh;min-height:180px;overflow:hidden;}
  #controls{padding:10px 12px calc(12px + env(safe-area-inset-bottom));}
  #route-meta{align-items:flex-start;}
  #route-sub{white-space:normal;overflow-wrap:anywhere;}
  #list-drawer{flex:1;min-height:0;}
  #route-list{flex:1;overflow-y:auto;padding:6px;}
  .route-card{padding:10px;margin-bottom:5px;}
}

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

/* ── Tabs ── */
#tabs{display:flex;gap:4px;margin-left:auto;}
.tab-btn{padding:5px 14px;border-radius:6px;border:1px solid #3f3f46;background:transparent;color:#a1a1aa;font-size:12px;cursor:pointer;touch-action:manipulation;transition:all 0.1s;}
.tab-btn.active{background:#22c55e;border-color:#22c55e;color:#000;font-weight:600;}
.tab-btn:active{background:#27272a;color:#fff;}

/* ── Live panel ── */
#live-panel{display:flex;flex-direction:column;flex:1;min-height:0;}
#live-video-wrap{flex:1;display:flex;align-items:center;justify-content:center;background:#000;min-height:0;position:relative;}
#live-video{width:100%;height:100%;object-fit:contain;display:block;background:#000;}
#live-canvas{position:absolute;pointer-events:none;z-index:2;}
#overlay-toggle{padding:5px 12px;border-radius:6px;border:1px solid #3f3f46;background:#27272a;color:#a1a1aa;font-size:12px;cursor:pointer;white-space:nowrap;transition:all 0.1s;touch-action:manipulation;}
#overlay-toggle.active{background:#22c55e;border-color:#22c55e;color:#000;font-weight:600;}
#live-controls{flex-shrink:0;background:#18181b;border-top:1px solid #27272a;padding:10px 14px;display:flex;flex-direction:column;gap:8px;}
#live-status{font-size:12px;color:#71717a;}
#live-status.connected{color:#22c55e;}
#live-status.connecting{color:#eab308;}
#live-status.error{color:#ef4444;}

/* ── Misc ── */
::-webkit-scrollbar{width:3px;height:3px;}
::-webkit-scrollbar-track{background:transparent;}
::-webkit-scrollbar-thumb{background:#3f3f46;border-radius:2px;}
#refresh-btn{padding:6px 12px;border-radius:8px;border:1px solid #3f3f46;background:transparent;color:#a1a1aa;font-size:12px;cursor:pointer;touch-action:manipulation;transition:all 0.1s;}
#refresh-btn:active{background:#27272a;color:#fff;}
</style>
</head>
<body>
<div id="app">

  <!-- Header -->
  <div id="header">
    <span style="color:#22c55e;font-size:10px;">●</span>
    <span style="font-size:16px;font-weight:600;letter-spacing:-0.3px;">Dashcam</span>
    <div id="tabs">
      <button class="tab-btn active" id="tab-live" onclick="switchTab('live')">Live</button>
      <button class="tab-btn" id="tab-dashcam" onclick="switchTab('dashcam')">Dashcam</button>
    </div>
    <button id="refresh-btn" onclick="refreshRoutes()">↺ Refresh</button>
  </div>

  <!-- Live Panel (default) -->
  <div id="live-panel">
    <div id="live-video-wrap">
      <video id="live-video" playsinline autoplay muted poster="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='640' height='360' viewBox='0 0 640 360'%3E%3Crect width='640' height='360' fill='%2309090b'/%3E%3Ccircle cx='320' cy='140' r='40' fill='none' stroke='%233f3f46' stroke-width='2'/%3E%3Cpolygon points='310,125 310,155 335,140' fill='%233f3f46'/%3E%3Ctext x='320' y='220' text-anchor='middle' fill='%2371717a' font-family='system-ui,sans-serif' font-size='16'%3ELive stream%3C/text%3E%3C/svg%3E"></video>
      <canvas id="live-canvas"></canvas>
      <div id="live-overlay" style="position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;color:#71717a;pointer-events:none;font-size:14px;z-index:3;">
        <svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M15 10l4.553-2.069A1 1 0 0121 8.882v6.236a1 1 0 01-1.447.894L15 14M4 8h8a2 2 0 012 2v4a2 2 0 01-2 2H4a2 2 0 01-2-2v-4a2 2 0 012-2z"/>
        </svg>
        <span id="live-overlay-text">Select a camera to start live stream</span>
      </div>
    </div>
    <div id="live-controls">
      <div id="live-status">Waiting…</div>
      <div style="display:flex;gap:5px;flex-wrap:wrap;align-items:center;">
        <button class="cam-btn active" id="live-cam-road" onclick="switchLiveCam('road')">Road</button>
        <button class="cam-btn" id="live-cam-driver" onclick="switchLiveCam('driver')">Driver</button>
        <button class="cam-btn" id="live-cam-wideRoad" onclick="switchLiveCam('wideRoad')">Wide</button>
        <button class="cam-btn active" id="overlay-toggle" onclick="toggleOverlay()">Overlay</button>
      </div>
    </div>
  </div>

  <!-- Dashcam Panel -->
  <div id="dashcam-panel" style="display:none;flex:1;min-height:0;flex-direction:column;">
    <div id="body">
      <!-- Player side -->
      <div id="player-area">
        <!-- Video -->
        <div id="video-wrap">
          <video id="video" playsinline controls preload="metadata" onended="onVideoEnded()" poster="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='640' height='360' viewBox='0 0 640 360'%3E%3Crect width='640' height='360' fill='%2309090b'/%3E%3Ccircle cx='320' cy='140' r='40' fill='none' stroke='%233f3f46' stroke-width='2'/%3E%3Cpolygon points='310,125 310,155 335,140' fill='%233f3f46'/%3E%3Ctext x='320' y='220' text-anchor='middle' fill='%2371717a' font-family='system-ui,sans-serif' font-size='16'%3ESelect a route%3C/text%3E%3C/svg%3E"></video>
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
</div>

<script>
const CAMERAS = [
  {key:'q', label:'Road (360p)'},
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
let currentStreamKey = '';
const routeDetailsCache = new Map();
const bookmarkCache = new Map();
let _routeController = null;
let _bookmarkController = null;
let _bookmarkFetchTimer = null;
let _initialPlayTimer = null;

// ── Tab switching ──────────────────────────────────────────────────────────
function switchTab(name) {
  document.getElementById('tab-live').classList.toggle('active', name === 'live');
  document.getElementById('tab-dashcam').classList.toggle('active', name === 'dashcam');
  document.getElementById('live-panel').style.display = name === 'live' ? 'flex' : 'none';
  document.getElementById('dashcam-panel').style.display = name === 'dashcam' ? 'flex' : 'none';
  if (name === 'live') {
    startLiveStream();
  } else {
    stopLiveStream();
  }
}

// ── Live stream (WebRTC) ─────────────────────────────────────────────────
let _livePc = null;
let _liveCam = 'road';
let _liveConnecting = false;
let _overlayEnabled = true;
let _turnConfig = null;

async function fetchTurnConfig() {
  try {
    const r = await fetch('/turn', {credentials: 'include'});
    if (r.ok) _turnConfig = await r.json();
  } catch (e) {}
}

function setLiveStatus(text, cls) {
  const el = document.getElementById('live-status');
  el.textContent = text;
  el.className = cls ? 'connected' : '';
  if (cls === 'connecting') el.className = 'connecting';
  if (cls === 'error') el.className = 'error';
}

function switchLiveCam(cam) {
  _liveCam = cam;
  ['road', 'driver', 'wideRoad'].forEach(c => {
    document.getElementById('live-cam-' + c).classList.toggle('active', c === cam);
  });
  // Restart stream with new camera
  if (_livePc || _liveConnecting) {
    stopLiveStream();
  }
  startLiveStream();
}

function toggleOverlay() {
  _overlayEnabled = !_overlayEnabled;
  const btn = document.getElementById('overlay-toggle');
  btn.classList.toggle('active', _overlayEnabled);
  btn.textContent = _overlayEnabled ? 'Overlay' : 'Overlay: Off';
  if (!_overlayEnabled) {
    const canvas = document.getElementById('live-canvas');
    if (canvas) {
      const ctx = canvas.getContext('2d');
      ctx.clearRect(0, 0, canvas.width, canvas.height);
    }
  }
}

function stopLiveStream() {
  if (_livePc) {
    _livePc.close();
    _livePc = null;
  }
  _liveConnecting = false;
  setLiveStatus('Waiting…');
}

async function startLiveStream() {
  if (_liveConnecting || _livePc) return;
  _liveConnecting = true;
  setLiveStatus('Connecting…', 'connecting');

  try {
    const pcConfig = {sdpSemantics: 'unified-plan'};
    if (_turnConfig && _turnConfig.iceServers) {
      pcConfig.iceServers = _turnConfig.iceServers;
    }
    const pc = new RTCPeerConnection(pcConfig);
    _livePc = pc;

    pc.addEventListener('track', function(evt) {
      if (evt.track.kind === 'video') {
        const video = document.getElementById('live-video');
        if (video.srcObject !== evt.streams[0]) {
          video.srcObject = evt.streams[0];
        }
        setLiveStatus('Live', 'connected');
        document.getElementById('live-overlay').style.display = 'none';
      }
    });

    // Data channel for modelV2 (lane lines / path)
    const dataCh = pc.createDataChannel('data', {ordered: true});
    dataCh.binaryType = 'arraybuffer';
    let _modelMsgCount = 0;
    dataCh.addEventListener('message', function(msg) {
      let text = msg.data;
      if (text instanceof ArrayBuffer) {
        text = new TextDecoder().decode(text);
      }
      try {
        const packet = JSON.parse(text);
        if (packet.type === 'modelV2' && packet.valid) {
          _modelMsgCount++;
          if (_modelMsgCount <= 3) console.log('modelV2 msg #' + _modelMsgCount, packet.data ? Object.keys(packet.data) : null);
          drawModelOverlay(packet.data);
        }
      } catch (e) {
        // ignore malformed messages
      }
    });
    dataCh.addEventListener('open', function() {
      console.log('Data channel open');
    });
    dataCh.addEventListener('close', function() {
      console.log('Data channel closed');
    });

    pc.addEventListener('iceconnectionstatechange', function() {
      if (pc.iceConnectionState === 'failed' || pc.iceConnectionState === 'disconnected') {
        setLiveStatus('Disconnected', 'error');
      }
    });

    // Add transceiver for receiving video
    pc.addTransceiver('video', {direction: 'recvonly'});

    const offer = await pc.createOffer();
    await pc.setLocalDescription(offer);

    // Wait for ICE gathering to complete (or timeout)
    await new Promise(function(resolve) {
      if (pc.iceGatheringState === 'complete') {
        resolve();
      } else {
        function checkState() {
          if (pc.iceGatheringState === 'complete') {
            pc.removeEventListener('icegatheringstatechange', checkState);
            resolve();
          }
        }
        pc.addEventListener('icegatheringstatechange', checkState);
        // Fallback timeout
        setTimeout(resolve, 2000);
      }
    });

    const r = await fetch('/offer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({sdp: pc.localDescription.sdp, type: pc.localDescription.type, cameras: [_liveCam]}),
      credentials: 'include',
    });

    if (!r.ok) {
      const text = await r.text();
      throw new Error('Offer failed: ' + text);
    }

    const answer = await r.json();
    // Guard against race: stopLiveStream() may have closed this pc while we were fetching
    if (pc.signalingState === 'closed') return;
    await pc.setRemoteDescription(answer);
  } catch (e) {
    console.error('Live stream error:', e);
    // Ignore expected abort when user switches cameras mid-handshake
    if (e?.message?.includes('signalingState is \'closed\'')) return;
    setLiveStatus('Error: ' + (e.message || 'failed'), 'error');
    if (_livePc === pc) { _livePc.close(); _livePc = null; }
  } finally {
    _liveConnecting = false;
  }
}

// ── Model overlay drawing ────────────────────────────────────────────────

function projectModelPoint(x, y, z, W, H) {
  // Proper pinhole projection from calib frame (x=forward, y=left, z=up) to screen pixels.
  // Camera: tici/mici road cam ox03c10, 1928x1208, focal=2648.
  if (x <= 0.5) return [W/2, H];

  const origW = 1928, origH = 1208, origF = 2648;
  const scale = Math.min(W / origW, H / origH);
  const f = origF * scale;
  const cx = W / 2;
  const cy = H / 2;

  // Typical windshield mount pitch (positive = camera looks down)
  const pitch = 0.015;
  const sinP = Math.sin(pitch);
  const cosP = Math.cos(pitch);

  // Camera is ~1.22m above road surface. Model z is relative to road.
  const camZ = 1.22;
  const calibX = x;
  const calibY = y;          // positive left
  const calibZ = (z || 0) + camZ;

  // calib -> device (forward, right, down)
  const devX = cosP * calibX + sinP * calibZ;
  const devY = calibY;
  const devZ = -sinP * calibX + cosP * calibZ;

  // device -> view (right, down, forward)
  const viewX = devY;
  const viewY = devZ;
  const viewZ = devX;

  if (viewZ <= 0.1) return [W/2, H];

  // calib y positive left -> image x decreases
  return [cx - f * (viewX / viewZ), cy + f * (viewY / viewZ)];
}

function _positionLiveCanvas() {
  const video = document.getElementById('live-video');
  const canvas = document.getElementById('live-canvas');
  const wrap = document.getElementById('live-video-wrap');
  if (!video || !canvas || !wrap) return;

  const vRect = video.getBoundingClientRect();
  const wRect = wrap.getBoundingClientRect();
  const left = vRect.left - wRect.left;
  const top = vRect.top - wRect.top;
  const vw = Math.round(vRect.width);
  const vh = Math.round(vRect.height);

  canvas.style.left = left + 'px';
  canvas.style.top = top + 'px';
  canvas.style.width = vw + 'px';
  canvas.style.height = vh + 'px';
  if (canvas.width !== vw || canvas.height !== vh) {
    canvas.width = vw;
    canvas.height = vh;
  }
  return { w: vw, h: vh };
}

function drawModelOverlay(model) {
  if (!_overlayEnabled) return;
  const sizing = _positionLiveCanvas();
  if (!sizing) return;
  const w = sizing.w, h = sizing.h;
  const canvas = document.getElementById('live-canvas');
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, w, h);

  function drawPolyline(xs, ys, zs, color, width, alpha, dashed) {
    if (!xs || !ys || xs.length < 2) return;
    const pts = [];
    const len = Math.min(xs.length, ys.length);
    for (let i = 0; i < len; i++) {
      if (xs[i] < 0) continue;
      const z = (zs && zs[i] != null) ? zs[i] : null;
      pts.push(projectModelPoint(xs[i], ys[i], z, w, h));
    }
    if (pts.length < 2) return;
    ctx.beginPath();
    ctx.moveTo(pts[0][0], pts[0][1]);
    for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
    ctx.lineCap = 'round';
    ctx.lineJoin = 'round';
    ctx.lineWidth = width;
    if (dashed) ctx.setLineDash(dashed);
    ctx.strokeStyle = color;
    ctx.stroke();
    if (dashed) ctx.setLineDash([]);
  }

  // --- Path (ego plan) ---
  const pos = model.position;
  if (pos && pos.x && pos.x.length && pos.y && pos.y.length) {
    const pts = [];
    const len = Math.min(pos.x.length, pos.y.length);
    for (let i = 0; i < len; i++) {
      if (pos.x[i] < 0) continue;
      const z = (pos.z && pos.z[i] != null) ? pos.z[i] : null;
      pts.push(projectModelPoint(pos.x[i], pos.y[i], z, w, h));
    }
    if (pts.length >= 2) {
      ctx.beginPath();
      ctx.moveTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length; i++) ctx.lineTo(pts[i][0], pts[i][1]);
      ctx.lineCap = 'round';
      ctx.lineJoin = 'round';
      ctx.lineWidth = Math.max(3, h / 180);
      ctx.strokeStyle = 'rgba(0, 255, 127, 0.85)';
      ctx.stroke();
      ctx.lineTo(pts[pts.length - 1][0], h);
      ctx.lineTo(pts[0][0], h);
      ctx.closePath();
      ctx.fillStyle = 'rgba(0, 255, 127, 0.12)';
      ctx.fill();
    }
  }

  // --- Lane lines ---
  const laneLines = model.laneLines;
  const laneProbs = model.laneLineProbs;
  if (laneLines && laneLines.length) {
    for (let i = 0; i < laneLines.length; i++) {
      const line = laneLines[i];
      const prob = (laneProbs && laneProbs[i] != null) ? laneProbs[i] : 1.0;
      if (prob < 0.3) continue;
      const lw = Math.max(1.5, h / 240);
      const a = 0.25 + prob * 0.6;
      drawPolyline(line.x, line.y, line.z, `rgba(255, 255, 255, ${a})`, lw, a, false);
    }
  }

  // --- Road edges ---
  const roadEdges = model.roadEdges;
  const roadStds = model.roadEdgeStds;
  if (roadEdges && roadEdges.length) {
    for (let i = 0; i < roadEdges.length; i++) {
      const edge = roadEdges[i];
      const std = (roadStds && roadStds[i] != null) ? roadStds[i] : 0;
      const a = Math.max(0, 0.7 - std * 2);
      if (a <= 0.05) continue;
      const lw = Math.max(1.5, h / 240);
      const dash = [Math.max(4, h / 120), Math.max(4, h / 120)];
      drawPolyline(edge.x, edge.y, edge.z, `rgba(239, 68, 68, ${a})`, lw, a, dash);
    }
  }

  // Debug counter
  ctx.font = `${Math.max(10, h / 36)}px monospace`;
  ctx.fillStyle = 'rgba(0,255,127,0.9)';
  ctx.fillText('model: ' + _modelMsgCount, 8, Math.max(16, h / 28));
}

// ── Resize handling for live canvas ──────────────────────────────────────

const _liveResizeObs = new ResizeObserver(function() {
  _positionLiveCanvas();
});

// ── Boot ─────────────────────────────────────────────────────────────────

// Observe live video wrap so canvas always matches displayed video bounds
(function() {
  const wrap = document.getElementById('live-video-wrap');
  if (wrap) _liveResizeObs.observe(wrap);
})();

async function loadRoutes() {
  try {
    const r = await fetch('/api/routes', {credentials: 'include'});
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
    return `<div class="route-card${sel}" data-route-id="${r.id}" onclick="selectRoute('${r.id}')">
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

function updateRouteSelection(routeId) {
  document.querySelectorAll('.route-card').forEach(card => {
    card.classList.toggle('selected', card.dataset.routeId === routeId);
  });
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

// Generation counter: incremented on every selectRoute call so that stale
// async completions (from a previous click) don't overwrite the current state.
let _selectGen = 0;

async function selectRoute(routeId) {
  const gen = ++_selectGen;
  clearTimeout(_bookmarkFetchTimer);
  clearTimeout(_initialPlayTimer);
  if (_routeController) _routeController.abort();
  if (_bookmarkController) _bookmarkController.abort();

  // Immediately stop the current video so the browser drops its
  // connections and stops background buffering / Range requests.
  const video = document.getElementById('video');
  if (video) {
    video.pause();
    video.removeAttribute('src');
    video.load();
  }
  currentStreamKey = null;

  let route = routeDetailsCache.get(routeId);
  if (!route) {
    let controller = null;
    try {
      controller = new AbortController();
      _routeController = controller;
      const r = await fetch(`/api/route/${routeId}`, {credentials: 'include', signal: controller.signal});
      if (!r.ok) throw new Error(r.status);
      route = await r.json();
      routeDetailsCache.set(routeId, route);
    } catch (e) {
      if (e?.name === 'AbortError') return;
      return;
    } finally {
      if (_routeController === controller) _routeController = null;
    }
  }
  if (gen !== _selectGen) return;  // superseded by a newer click

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

  updateRouteSelection(routeId);

  routeBookmarks = bookmarkCache.get(routeId) || {};
  renderSegStrip();
  updateBookmarkRow(currentRoute?.segments[currentSegIdx]);

  _initialPlayTimer = setTimeout(() => {
    if (gen === _selectGen) playSegment(0);
  }, 120);

  if (bookmarkCache.has(routeId)) return;

  _bookmarkFetchTimer = setTimeout(() => {
    if (gen !== _selectGen) return;
    const controller = new AbortController();
    _bookmarkController = controller;
    fetch(`/api/bookmarks/${routeId}`, {credentials: 'include', signal: controller.signal})
      .then(r => r.ok ? r.json() : {})
      .then(bm => {
        if (gen !== _selectGen) return;
        bookmarkCache.set(routeId, bm);
        routeBookmarks = bm;
        renderSegStrip();
        updateBookmarkRow(currentRoute?.segments[currentSegIdx]);
      })
      .catch(e => {
        if (e?.name !== 'AbortError') {}
      })
      .finally(() => {
        if (_bookmarkController === controller) _bookmarkController = null;
      });
  }, 250);
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
  if (idx === currentSegIdx) return;
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
  if (currentCam === key) return;
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
  const nextStreamKey = `${currentRoute.id}/${seg}/${currentCam}`;

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

  if (currentStreamKey === nextStreamKey) {
    updateBookmarkRow(seg);
    return;
  }

  // Load and play video — each stream now includes its own audio track.
  const video = document.getElementById('video');
  video.pause();
  video.removeAttribute('src');
  video.load();
  currentStreamKey = nextStreamKey;
  video.src = `/stream/${nextStreamKey}`;
  video.load();

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

switchTab('live');
loadRoutes();
fetchTurnConfig();
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# WebRTC offer proxy — forwards browser SDP to webrtcd
# ---------------------------------------------------------------------------

class _StreamRequestBody:
    def __init__(self, sdp: str, cameras: list[str], bridge_services_in: list[str] = None, bridge_services_out: list[str] = None):
        self.sdp = sdp
        self.cameras = cameras
        self.bridge_services_in = bridge_services_in or []
        self.bridge_services_out = bridge_services_out or []

    def to_dict(self):
        return {
            "sdp": self.sdp,
            "cameras": self.cameras,
            "bridge_services_in": self.bridge_services_in,
            "bridge_services_out": self.bridge_services_out,
        }


async def handle_turn(request: web.Request) -> web.Response:
    """Return TURN server configuration for WebRTC relay through NAT/proxy."""
    urls = os.environ.get("TURN_URLS", "")
    username = os.environ.get("TURN_USER", "")
    credential = os.environ.get("TURN_PASS", "")
    ice_servers = []
    if urls:
        ice_servers.append({
            "urls": urls.split(","),
            "username": username,
            "credential": credential,
        })
    return web.json_response({"iceServers": ice_servers})


async def handle_offer(request: web.Request) -> web.Response:
    """Receive browser WebRTC offer, proxy to webrtcd, return answer."""
    try:
        params = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Invalid JSON")

    # camera name mapping: dashcam UI -> webrtcd LiveStreamVideoStreamTrack
    cam_map = {
        "road": "road",
        "driver": "driver",
        "wideRoad": "wideRoad",
    }
    requested = params.get("cameras", ["road"])
    cameras = [cam_map.get(c, c) for c in requested if c in cam_map]
    if not cameras:
        cameras = ["road"]

    body = _StreamRequestBody(params.get("sdp", ""), cameras, bridge_services_out=["modelV2"])
    body_json = json.dumps(body.to_dict())

    webrtcd_url = f"http://{WEBRTCD_HOST}:{WEBRTCD_PORT}/stream"
    try:
        async with ClientSession() as session:
            async with session.post(webrtcd_url, data=body_json, timeout=ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise web.HTTPBadGateway(text=f"webrtcd returned {resp.status}: {text[:200]}")
                answer = await resp.json()
                return web.json_response(answer)
    except asyncio.TimeoutError:
        raise web.HTTPGatewayTimeout(text="webrtcd timed out")
    except web.HTTPException:
        raise
    except Exception as e:
        err_str = str(e)
        if "Cannot connect to host" in err_str or "Connection refused" in err_str:
            raise web.HTTPServiceUnavailable(text="Live stream unavailable while offroad")
        raise web.HTTPInternalServerError(text=f"webrtc proxy error: {e}")


async def handle_debug(request: web.Request) -> web.Response:
    import subprocess
    # Test ffmpeg directly
    result = subprocess.run([_FFMPEG, '-version'], capture_output=True, text=True)
    version = result.stdout.split('\n')[0] if result.stdout else 'NO OUTPUT'
    return web.json_response({
        'ffmpeg': _FFMPEG,
        'ffprobe': _FFPROBE,
        'ffmpeg_exists': os.path.exists(_FFMPEG) if _FFMPEG else False,
        'ffprobe_exists': os.path.exists(_FFPROBE) if _FFPROBE else False,
        'ffmpeg_version': version,
        'ffmpeg_stderr': result.stderr[:200] if result.stderr else '',
    })


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    import logging
    logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

    # ── Basic auth middleware ──
    _expected = base64.b64encode(f"{DASHCAM_USER}:{DASHCAM_PASS}".encode()).decode()

    @web.middleware
    async def basic_auth(request: web.Request, handler):
        auth = request.headers.get('Authorization', '')
        if auth == f'Basic {_expected}':
            return await handler(request)
        return web.Response(
            status=401,
            headers={'WWW-Authenticate': 'Basic realm="Dashcam"'},
            text='Unauthorized',
        )

    app = web.Application(middlewares=[basic_auth])
    app.router.add_get('/',                                  handle_index)
    app.router.add_get('/api/routes',                        handle_api_routes)
    app.router.add_get('/api/route/{route_id}',              handle_api_route)
    app.router.add_get('/stream/{route_id}/{segment}',       handle_stream)
    app.router.add_get('/stream/{route_id}/{segment}/{camera}', handle_stream)
    app.router.add_get('/api/bookmarks/{route_id}',           handle_api_bookmarks)
    app.router.add_get('/turn',                               handle_turn)
    app.router.add_post('/offer',                             handle_offer)
    app.router.add_get('/_debug',                            handle_debug)

    # Try HTTPS first (needed for WebRTC on non-localhost); fall back to plain HTTP.
    ssl_ctx = _ensure_ssl_cert()
    if ssl_ctx is not None:
        try:
            web.run_app(app, host='0.0.0.0', port=PORT, ssl_context=ssl_ctx, access_log=None)
            return
        except Exception:
            pass
    web.run_app(app, host='0.0.0.0', port=PORT, access_log=None)


if __name__ == '__main__':
    main()
