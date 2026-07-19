"""iPodSync web UI — configure, start, and monitor syncs.

Single background worker (a sync is serial). Progress from engine.sync is captured
as JSON-line events and streamed to the browser via Server-Sent Events.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse, Response

from engine.sync import Config, run_sync
from engine.mount import (
    mount_ipod, eject_ipod, is_mounted, find_partition, whole_disk, MountError,
)

CONFIG_DIR = os.environ.get("IPODSYNC_CONFIG", "/config")
SETTINGS_PATH = os.path.join(CONFIG_DIR, "settings.json")

DEFAULTS = {
    "music": os.environ.get("IPODSYNC_MUSIC", "/music"),
    "ipod": os.environ.get("IPODSYNC_IPOD", "/ipod"),
    "label": os.environ.get("IPODSYNC_LABEL", "IPOD"),
    "device": os.environ.get("IPODSYNC_DEVICE", ""),        # optional override
    "partition": os.environ.get("IPODSYNC_PARTITION", ""),  # optional override
    "encoder": "alac",
    "threads": 0,          # 0 = gpod-cp default (all vCPUs)
    "prune": True,
    "auto_sync": True,     # sync automatically when the iPod is connected
    "auto_eject": True,    # eject automatically after a successful sync
}


# Set on eject, cleared when the iPod actually goes away (or on an explicit Mount).
# Without it the watcher re-mounts within 5s: a SCSI-stopped iPod keeps its block node
# and by-label link until it is physically unplugged, so "ejected" would never stick.
_EJECT_HOLD = False


def resolve_devices(s: dict) -> tuple[str, str]:
    """(partition, whole_disk). Prefer explicit settings when the node exists, else
    auto-detect the iPod by label — so the container starts with no iPod attached and
    survives device-node changes across replugs."""
    part = s.get("partition") or ""
    if not part or not os.path.exists(part):
        part = find_partition(s.get("label", "IPOD"))
    dev = s.get("device") or ""
    if not dev or not os.path.exists(dev):
        dev = whole_disk(part) if part else ""
    return part, dev


def load_settings() -> dict:
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH) as f:
            s.update(json.load(f))
    except FileNotFoundError:
        pass
    return s


def save_settings(s: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(s, f, indent=2)


class Job:
    """Holds the running/last sync: its event log and status."""
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        # Hold a whole sync's events so /api/events index-based polling stays aligned
        # (a full-library sync emits one add_log line per file, ~20k+).
        self.events: deque[dict] = deque(maxlen=60000)
        self.running = False
        self.kind = ""            # "sync" | "dry-run"
        self.started_at: Optional[float] = None
        self.summary: Optional[dict] = None

    def is_running(self) -> bool:
        return self.running

    def start(self, kind: str, cfg: Config, dry_run: bool) -> None:
        with self.lock:
            if self.running:
                raise RuntimeError("a job is already running")
            self.events.clear()
            self.running = True
            self.kind = kind
            self.started_at = time.time()
            self.summary = None

        def _emit(obj: dict) -> None:
            obj["t"] = round(time.time(), 3)
            self.events.append(obj)

        def _work() -> None:
            try:
                summary = run_sync(cfg, emit=_emit, dry_run=dry_run)
                self.summary = summary
                if not dry_run and summary.get("event") == "done":
                    (Path(CONFIG_DIR) / "last_sync.json").write_text(json.dumps(summary))
                    if load_settings().get("auto_eject", True):
                        _auto_eject_after_sync(_emit)
            except Exception as e:  # surface engine errors into the stream
                self.events.append({"event": "error", "message": str(e), "t": time.time()})
            finally:
                self.running = False

        self.thread = threading.Thread(target=_work, daemon=True)
        self.thread.start()


JOB = Job()
app = FastAPI(title="iPodSync")


def _cfg_from_settings() -> Config:
    s = load_settings()
    threads = s.get("threads") or None
    _part, device = resolve_devices(s)
    return Config(
        music=s["music"], ipod=s["ipod"], device=device or None,
        encoder=s.get("encoder", "alac"), threads=threads,
        prune=bool(s.get("prune", True)), config_dir=CONFIG_DIR,
    )


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/settings")
def get_settings():
    return load_settings()


@app.post("/api/settings")
async def post_settings(payload: dict):
    s = load_settings()
    for k in ("music", "ipod", "label", "device", "partition", "encoder"):
        if k in payload:
            s[k] = str(payload[k])
    if "threads" in payload:
        s["threads"] = int(payload["threads"] or 0)
    for b in ("prune", "auto_sync", "auto_eject"):
        if b in payload:
            s[b] = bool(payload[b])
    save_settings(s)
    return s


def _auto_eject_after_sync(emit) -> None:
    """Eject after a successful sync so the iPod is ready to unplug (hands-off flow)."""
    global _EJECT_HOLD
    s = load_settings()
    _part, dev = resolve_devices(s)
    try:
        result = eject_ipod(s["ipod"], dev)
        _EJECT_HOLD = True
        emit({"event": "auto_eject", "result": result})
    except MountError as e:
        emit({"event": "auto_eject_failed", "message": str(e)})


def _device_watcher() -> None:
    """Detect the iPod being connected (partition appears by label), mount it, and — if
    enabled — start one sync per connect. `armed` is set while the iPod is absent and
    cleared once a connect has been serviced, so it fires once per plug-in and never on a
    container restart with the iPod already attached (armed starts False when present).
    After an eject the watcher stands down until the device is gone, so auto-mode never
    remounts an iPod the user just told it to release."""
    global _EJECT_HOLD
    armed = not bool(resolve_devices(load_settings())[0])
    while True:
        time.sleep(5)
        try:
            s = load_settings()
            part, _dev = resolve_devices(s)
            if not part:                     # disconnected — re-arm for next connect
                armed = True
                _EJECT_HOLD = False
                continue
            if _EJECT_HOLD:                  # ejected, still plugged in — leave it alone
                continue
            if not is_mounted(s["ipod"]):
                try:
                    mount_ipod(part, s["ipod"])
                except MountError:
                    continue                 # not ready yet; retry next tick
            if armed and s.get("auto_sync", True) and not JOB.is_running():
                try:
                    JOB.start("auto", _cfg_from_settings(), dry_run=False)
                    armed = False
                except RuntimeError:
                    pass
        except Exception:
            pass


@app.on_event("startup")
def _startup():
    """Container owns the mount: mount the iPod on startup if attached (absent is fine —
    the container must start regardless), and launch the connect watcher."""
    s = load_settings()
    part, _dev = resolve_devices(s)
    if part and not is_mounted(s["ipod"]):
        try:
            mount_ipod(part, s["ipod"])
        except MountError:
            pass
    threading.Thread(target=_device_watcher, daemon=True).start()


@app.post("/api/mount")
def api_mount():
    global _EJECT_HOLD
    s = load_settings()
    part, _dev = resolve_devices(s)
    if not part:
        raise HTTPException(400, "iPod not detected (no partition with the configured label; connect it)")
    try:
        did = mount_ipod(part, s["ipod"])
        _EJECT_HOLD = False      # explicit Mount overrides a prior eject
    except MountError as e:
        raise HTTPException(400, str(e))
    return {"mounted": True, "changed": did}


@app.post("/api/eject")
def api_eject():
    global _EJECT_HOLD
    if JOB.is_running():
        raise HTTPException(409, "a sync is running; wait for it to finish before ejecting")
    s = load_settings()
    _part, dev = resolve_devices(s)
    try:
        result = eject_ipod(s["ipod"], dev)
        _EJECT_HOLD = True
    except MountError as e:
        raise HTTPException(400, str(e))
    return {"ejected": True, **result}


@app.get("/api/status")
def status():
    s = load_settings()
    mount = s["ipod"]
    mounted = is_mounted(mount)
    connected = os.path.isdir(os.path.join(mount, "iPod_Control"))
    part, _dev = resolve_devices(s)
    st: dict = {
        "connected": connected,
        "mounted": mounted,
        "partition_present": bool(part),
        "ejected": _EJECT_HOLD and not mounted,
        "device": part,
        "running": JOB.is_running(),
        "job_kind": JOB.kind,
        "last_summary": JOB.summary,
    }
    if connected:
        try:
            vfs = os.statvfs(mount)
            st["capacity_bytes"] = vfs.f_blocks * vfs.f_frsize
            st["free_bytes"] = vfs.f_bavail * vfs.f_frsize
        except OSError:
            pass
        sysinfo = Path(mount) / "iPod_Control" / "Device" / "SysInfoExtended"
        st["sysinfo_extended"] = sysinfo.exists() and sysinfo.stat().st_size > 0
    lp = Path(CONFIG_DIR) / "last_sync.json"
    if lp.exists():
        try:
            st["last_sync"] = json.loads(lp.read_text())
        except (OSError, ValueError):
            pass
    return st


@app.post("/api/sync")
def start_sync(dry_run: bool = False):
    if JOB.is_running():
        raise HTTPException(409, "a job is already running")
    cfg = _cfg_from_settings()
    if not os.path.isdir(os.path.join(cfg.ipod, "iPod_Control")):
        raise HTTPException(400, "iPod not connected (no iPod_Control at mount)")
    JOB.start("dry-run" if dry_run else "sync", cfg, dry_run)
    return {"started": True, "dry_run": dry_run}


@app.get("/api/events")
def events(since: int = 0):
    evs = list(JOB.events)
    return JSONResponse({"running": JOB.is_running(), "events": evs[since:], "next": len(evs)})


@app.get("/api/stream")
def stream():
    def gen():
        idx = 0
        while True:
            evs = list(JOB.events)
            while idx < len(evs):
                yield f"data: {json.dumps(evs[idx])}\n\n"
                idx += 1
            if not JOB.is_running() and idx >= len(evs):
                yield f"data: {json.dumps({'event': 'stream_end'})}\n\n"
                return
            time.sleep(0.5)
    return StreamingResponse(gen(), media_type="text/event-stream")


_STATIC = Path(__file__).parent / "static"


@app.get("/", response_class=HTMLResponse)
def index():
    return (_STATIC / "index.html").read_text()


@app.get("/icon.svg")
def icon_svg():
    return Response((_STATIC / "icon.svg").read_text(), media_type="image/svg+xml")


@app.get("/icon.png")
def icon_png():
    return Response((_STATIC / "icon.png").read_bytes(), media_type="image/png")


@app.get("/icon-180.png")
def icon_180():
    return Response((_STATIC / "icon-180.png").read_bytes(), media_type="image/png")


@app.get("/manifest.webmanifest")
def manifest():
    return JSONResponse({
        "name": "iPodSync", "short_name": "iPodSync", "display": "standalone",
        "background_color": "#0f1115", "theme_color": "#0f1115", "start_url": "/",
        "icons": [
            {"src": "/icon.png", "sizes": "256x256", "type": "image/png"},
            {"src": "/icon.svg", "sizes": "any", "type": "image/svg+xml"},
        ],
    })
