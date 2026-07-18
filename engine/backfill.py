"""One-off: backfill missing ipod_path in the sync state.

Older syncs mapped added files back to the iPod by full identity
(artist/album/title/duration), which misses transcoded files whose duration drifted.
Those rows have ipod_path = NULL, so a later sync would prune and re-add them.

This matches each such row to its current on-iPod track by a looser key
(artist/album/title, no duration) and fills ipod_path — but only when exactly one
unclaimed iPod track matches, so it never guesses. Anything left unmapped simply
re-syncs once (the current gpod-cp log parser then maps it exactly).

    python3 -m engine.backfill [--ipod /ipod] [--config /config]
"""
from __future__ import annotations

import argparse
import os
from collections import defaultdict

from .ipod import Ipod
from .state import State


def _norm(s: str) -> str:
    return " ".join((s or "").strip().lower().split())


def _loose(identity: str) -> str:
    # identity_key = artist\x1f album\x1f title\x1f seconds. Key on (album, title) only:
    # artist drifts on compilations (iPod drops albumartist) and duration drifts on
    # transcodes, but album+title is stable. Uniqueness is enforced by the caller.
    p = identity.split("\x1f")
    return f"{p[1]}\x1f{p[2]}" if len(p) >= 3 else identity


def _loose_from_track(t) -> str:
    return f"{_norm(t.album)}\x1f{_norm(t.title)}"


def backfill(ipod_mount: str, config_dir: str) -> dict:
    state = State(os.path.join(config_dir, "state.sqlite"))
    ipod = Ipod(ipod_mount)
    tracks = ipod.list_tracks()

    claimed = {
        r[0] for r in state.con.execute(
            "SELECT ipod_path FROM synced WHERE ipod_path IS NOT NULL"
        )
    }
    index: dict[str, list] = defaultdict(list)
    for t in tracks:
        if t.ipod_path not in claimed:
            index[_loose_from_track(t)].append(t)

    rows = state.con.execute(
        "SELECT src_path, identity FROM synced WHERE ipod_path IS NULL"
    ).fetchall()

    filled = ambiguous = unmatched = 0
    for src_path, identity in rows:
        cands = index.get(_loose(identity), [])
        avail = [t for t in cands if t.ipod_path not in claimed]
        if len(avail) == 1:
            ip = avail[0].ipod_path
            state.con.execute("UPDATE synced SET ipod_path=? WHERE src_path=?", (ip, src_path))
            claimed.add(ip)
            filled += 1
        elif len(avail) > 1:
            ambiguous += 1
        else:
            unmatched += 1
    state.commit()
    state.close()
    return {"null_rows": len(rows), "filled": filled, "ambiguous": ambiguous, "unmatched": unmatched}


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Backfill ipod_path in sync state")
    p.add_argument("--ipod", default=os.environ.get("IPODSYNC_IPOD", "/ipod"))
    p.add_argument("--config", default=os.environ.get("IPODSYNC_CONFIG", "/config"))
    args = p.parse_args(argv)
    print(backfill(args.ipod, args.config))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
