"""Incremental iPod sync.

Reads the source library and the iPod's current iTunesDB, computes a delta, then
copies new/changed files (gpod-cp, FLAC->ALAC) and — when pruning — removes iPod
tracks no longer backed by a source file (gpod-rm).

First run against an iTunes-built DB has no state mapping, so unchanged tracks are
reconciled by identity (artist/album/title/duration) and kept without re-copying.

Progress is emitted as JSON lines via `emit`, so the web UI (or a terminal) can
stream it. Run as a module:

    python3 -m engine.sync --music /music --ipod /ipod --device /dev/sdg [--dry-run] [--no-prune]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from .ipod import Ipod, SourceFile, Track, read_source_file, stat_walk
from .state import State


Emit = Callable[[dict], None]

# gpod-cp per-file line: "[  n/N]  <source> -> { ... ipod_path='<ipod_path>' }"
# (gpod-cp.c: g_print("[%3u/%u]  %s -> %s"), braces via gpod_cp_log ipod_path='%s').
# Empty ipod_path (failed/DUPL) -> group(2) empty -> skip.
ADD_LINE_RE = re.compile(r"^\[\s*\d+/\s*\d+\]\s+(.*?)\s+->\s+\{.*?ipod_path='([^']*)'")

# Known-harmless libgpod/ffmpeg chatter. Non-fatal; hidden from the streamed log to
# avoid false alarms. itdb_splr_validate fires on a pre-existing on-iPod smart playlist
# whose rule type libgpod doesn't recognize — it is skipped, not corrupted.
BENIGN_RE = re.compile(
    r"itdb_splr_validate|timescale not set|Estimating duration|"
    r"deprecated pixel format|Could not find codec parameters",
    re.I,
)


def _stdout_emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


@dataclass
class Plan:
    to_add: list[SourceFile] = field(default_factory=list)
    to_remove: list[Track] = field(default_factory=list)
    reconciled: int = 0          # existing iPod tracks matched to a source (kept, not copied)
    unchanged: int = 0           # already tracked in state, present, unmodified
    source_count: int = 0
    ipod_count: int = 0


@dataclass
class Config:
    music: str
    ipod: str
    device: Optional[str] = None
    encoder: str = "alac"
    threads: Optional[int] = None
    prune: bool = True
    config_dir: str = "/config"


def compute_plan(cfg: Config, ipod: Ipod, state: State, emit: Emit) -> Plan:
    emit({"event": "scan_ipod_start"})
    ipod_tracks = ipod.list_tracks()
    by_path: dict[str, Track] = {t.ipod_path: t for t in ipod_tracks}
    by_identity: dict[str, list[Track]] = defaultdict(list)
    for t in ipod_tracks:
        by_identity[t.identity].append(t)
    emit({"event": "scan_ipod_done", "count": len(ipod_tracks)})

    plan = Plan(ipod_count=len(ipod_tracks))
    claimed: set[str] = set()          # ipod_paths backed by a current source file
    seen_src: set[str] = set()

    emit({"event": "scan_source_start", "root": cfg.music})
    tags_read = 0
    for path, mtime, size in stat_walk(cfg.music):
        seen_src.add(path)
        plan.source_count += 1
        if plan.source_count % 2000 == 0:
            emit({"event": "scan_source_progress", "count": plan.source_count})

        # Fast path: file already recorded, unchanged, still present on iPod — no tag read.
        row = state.get(path)
        if (
            row is not None
            and row.src_mtime == mtime
            and row.src_size == size
            and row.ipod_path in by_path
            and row.ipod_path not in claimed
        ):
            claimed.add(row.ipod_path)
            plan.unchanged += 1
            continue

        # New/changed: read tags now to get identity for reconcile/add.
        src = read_source_file(path, mtime, size)
        tags_read += 1

        match = _claim_by_identity(by_identity, src.identity, claimed)
        if match is not None:
            claimed.add(match.ipod_path)
            state.upsert(src.path, src.mtime, src.size, src.identity, match.ipod_path)
            plan.reconciled += 1
            continue

        plan.to_add.append(src)

    emit({"event": "scan_source_done", "count": plan.source_count, "tags_read": tags_read})

    # Source files that vanished: drop their state rows (their iPod track, if any,
    # is now unclaimed and handled by prune below).
    stale = state.all_paths() - seen_src
    if stale:
        state.delete(stale)
        emit({"event": "state_pruned", "count": len(stale)})

    if cfg.prune:
        plan.to_remove = [t for t in ipod_tracks if t.ipod_path not in claimed]

    state.commit()
    emit({
        "event": "plan",
        "to_add": len(plan.to_add),
        "to_remove": len(plan.to_remove),
        "reconciled": plan.reconciled,
        "unchanged": plan.unchanged,
        "source_count": plan.source_count,
        "ipod_count": plan.ipod_count,
    })
    return plan


def _claim_by_identity(
    by_identity: dict[str, list[Track]], identity: str, claimed: set[str]
) -> Optional[Track]:
    for t in by_identity.get(identity, ()):
        if t.ipod_path not in claimed:
            return t
    return None


def run_sync(cfg: Config, emit: Emit = _stdout_emit, dry_run: bool = False) -> dict:
    started = time.time()
    emit({"event": "start", "music": cfg.music, "ipod": cfg.ipod, "prune": cfg.prune, "dry_run": dry_run})

    ipod = Ipod(cfg.ipod, cfg.device)
    if ipod.ensure_sysinfo_extended():
        emit({"event": "sysinfo_extended_created"})

    os.makedirs(cfg.config_dir, exist_ok=True)
    state = State(os.path.join(cfg.config_dir, "state.sqlite"))

    try:
        plan = compute_plan(cfg, ipod, state, emit)

        if dry_run:
            emit({"event": "dry_run_complete"})
            summary = _summary(plan, started, dry_run=True)
            emit(summary)          # terminal event so streamers see completion
            return summary

        if plan.to_add:
            emit({"event": "add_start", "count": len(plan.to_add)})
            paths = [s.path for s in plan.to_add]
            # gpod-cp prints one line per file: "[ n/N]  <source> -> { ... ipod_path='<p>' }".
            # Parse it for an exact source->ipod_path mapping (transcoded files drift in
            # duration, so identity re-matching is unreliable — the log is authoritative).
            added_map: dict[str, str] = {}

            def _on_add(ln: str) -> None:
                if not BENIGN_RE.search(ln):
                    emit({"event": "add_log", "line": ln})
                m = ADD_LINE_RE.match(ln)
                if m and m.group(2):
                    added_map[m.group(1)] = m.group(2)

            ipod.add(paths, encoder=cfg.encoder, threads=cfg.threads, on_line=_on_add)
            _record_added(state, plan, added_map, emit)
            emit({"event": "add_done"})

        if plan.to_remove:
            emit({"event": "remove_start", "count": len(plan.to_remove)})
            ipod.remove(
                [t.ipod_path for t in plan.to_remove],
                on_line=lambda ln: None if BENIGN_RE.search(ln)
                else emit({"event": "remove_log", "line": ln}),
            )
            emit({"event": "remove_done"})

        state.commit()
        emit({"event": "verify_start"})
        vout = ipod.verify()
        emit({"event": "verify_done", "output": vout.strip()[-500:]})
        summary = _summary(plan, started, dry_run=False)
        emit(summary)              # terminal event so streamers see completion
        return summary
    finally:
        state.close()


def _record_added(state: State, plan: Plan, added_map: dict[str, str], emit: Emit) -> None:
    """Record each added file's exact ipod_path (from gpod-cp's log) so future runs
    match it by path+mtime+size and skip it — genuine incrementality. Files with no
    ipod_path (transcode failed / skipped) get no state row and retry next run."""
    mapped = 0
    for src in plan.to_add:
        ipod_path = added_map.get(src.path)
        if ipod_path:
            state.upsert(src.path, src.mtime, src.size, src.identity, ipod_path)
            mapped += 1
    state.commit()
    emit({"event": "add_mapped", "mapped": mapped, "total": len(plan.to_add)})


def _summary(plan: Plan, started: float, dry_run: bool) -> dict:
    return {
        "event": "done",
        "dry_run": dry_run,
        "added": len(plan.to_add),
        "removed": len(plan.to_remove),
        "reconciled": plan.reconciled,
        "unchanged": plan.unchanged,
        "source_count": plan.source_count,
        "ipod_count": plan.ipod_count,
        "elapsed_s": round(time.time() - started, 1),
    }


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Incremental iPod sync")
    p.add_argument("--music", default=_env("IPODSYNC_MUSIC", "/music"))
    p.add_argument("--ipod", default=_env("IPODSYNC_IPOD", "/ipod"))
    p.add_argument("--config", default=_env("IPODSYNC_CONFIG", "/config"))
    p.add_argument("--device", default=os.environ.get("IPODSYNC_DEVICE"))
    p.add_argument("--encoder", default="alac")
    p.add_argument("--threads", type=int, default=None)
    p.add_argument("--no-prune", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    cfg = Config(
        music=args.music, ipod=args.ipod, device=args.device,
        encoder=args.encoder, threads=args.threads,
        prune=not args.no_prune, config_dir=args.config,
    )
    summary = run_sync(cfg, dry_run=args.dry_run)
    return 0 if summary.get("event") == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
