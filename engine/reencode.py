"""One-off repair: re-transcode on-iPod tracks written above the 48 kHz ALAC ceiling.

Builds before the switch to the d3vil-st gpod-utils fork used upstream's
_select_samplerate(), which returns the *input* rate whenever the encoder publishes
no rate list — and ffmpeg's alac encoder publishes none. So a 96/176.4/192 kHz FLAC
was written to the iPod as ALAC at that rate, which the Classic firmware cannot
decode. The current build caps output at 48 kHz, but the source files are unchanged,
so the delta engine sees these tracks as up to date and will never revisit them.

Note the iTunesDB stores samplerate in 16 bits, so the offending rates wrap:
96000 -> 30464, 176400 -> 45328, 192000 -> 60928. Detection keys on "wrapped or
above the ceiling", not on the literal rate.

For each affected track: resolve its source via the sync state, remove the iPod
track, re-add the source (transcoded correctly this time), and repoint the state
row at the new ipod_path.

    python3 -m engine.reencode [--dry-run] [--limit N] [--ipod /ipod] [--config /config]
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile

from .ipod import Ipod, IpodError
from .state import State
from .sync import ADD_LINE_RE, BENIGN_RE

MAX_SAMPLERATE = 48000

# Rates that a 16-bit iTunesDB field wraps to. Anything not a plausible audio rate
# is suspect, but these are the ones our own transcodes can produce.
WRAPPED = {96000 & 0xFFFF: 96000, 176400 & 0xFFFF: 176400, 192000 & 0xFFFF: 192000}


def find_bad(db_path: str) -> list[dict]:
    """Tracks whose stored samplerate is above the ceiling or a known wrap of it."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    out = []
    for r in con.execute("SELECT id, ipod_path, artist, album, title, samplerate FROM tracks"):
        sr = int(r["samplerate"] or 0)
        real = WRAPPED.get(sr, sr if sr > MAX_SAMPLERATE else 0)
        if real:
            d = dict(r)
            d["real_samplerate"] = real
            out.append(d)
    con.close()
    return out


def reencode(ipod_mount: str, config_dir: str, *, encoder: str = "alac",
          threads: int = 0, limit: int = 0, dry_run: bool = False,
          log=print) -> dict:
    ipod = Ipod(ipod_mount)
    state = State(os.path.join(config_dir, "state.sqlite"))
    try:
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "ipod.sqlite3")
            from .ipod import _run
            _run(["gpod-ls", "-M", ipod_mount, "-Q", db, "--disable-checksum"])
            bad = find_bad(db)

        by_path = {
            r[0]: r[1] for r in state.con.execute(
                "SELECT ipod_path, src_path FROM synced WHERE ipod_path IS NOT NULL"
            )
        }
        work = [(b, by_path[b["ipod_path"]]) for b in bad if b["ipod_path"] in by_path]
        orphans = len(bad) - len(work)
        if limit:
            work = work[:limit]

        log({"event": "reencode_plan", "found": len(bad), "resolved": len(work),
             "orphans": orphans, "dry_run": dry_run})
        if dry_run or not work:
            return {"found": len(bad), "resolved": len(work), "orphans": orphans,
                    "removed": 0, "readded": 0, "dry_run": dry_run}

        # Remove first: gpod-cp -r Y matches on title/album/artist, which is looser
        # than we want here and would leave the old track behind on a near-miss.
        ipod.remove([b["ipod_path"] for b, _ in work],
                    on_line=lambda ln: None if BENIGN_RE.search(ln) else log({"event": "reencode_log", "line": ln}))

        added_map: dict[str, str] = {}

        def _on_add(ln: str) -> None:
            if not BENIGN_RE.search(ln):
                log({"event": "reencode_log", "line": ln})
            m = ADD_LINE_RE.match(ln)
            if m and m.group(2):
                added_map[m.group(1)] = m.group(2)

        srcs = [src for _b, src in work]
        ipod.add(srcs, encoder=encoder, threads=threads or None, replace=False, on_line=_on_add)

        readded = 0
        for _b, src in work:
            new_path = added_map.get(src)
            # No new path means the copy failed; drop the stale mapping so the next
            # normal sync treats the file as new and retries it.
            state.con.execute("UPDATE synced SET ipod_path=? WHERE src_path=?", (new_path, src))
            readded += bool(new_path)
        state.commit()

        return {"found": len(bad), "resolved": len(work), "orphans": orphans,
                "removed": len(work), "readded": readded, "dry_run": False}
    finally:
        state.close()


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Re-transcode iPod tracks above the 48 kHz ALAC ceiling")
    p.add_argument("--ipod", default=os.environ.get("IPODSYNC_IPOD", "/ipod"))
    p.add_argument("--config", default=os.environ.get("IPODSYNC_CONFIG", "/config"))
    p.add_argument("--encoder", default="alac")
    p.add_argument("--threads", type=int, default=0)
    p.add_argument("--limit", type=int, default=0, help="only repair the first N (try a small batch first)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)
    print(reencode(args.ipod, args.config, encoder=args.encoder, threads=args.threads,
                limit=args.limit, dry_run=args.dry_run, log=lambda o: print(o, flush=True)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
