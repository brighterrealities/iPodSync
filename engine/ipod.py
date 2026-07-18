"""Thin wrappers around gpod-utils (libgpod) CLI tools.

gpod-cp   -M <mount> [-e alac] [-T n] [-r Y|N] <files...>   add/transcode into the iTunesDB
gpod-ls   -M <mount> -Q <sqlite>  --disable-checksum        dump current DB as a SQLite `tracks` table
gpod-rm   -M <mount> <ipod_path|id> ...                     remove tracks
gpod-verify -M <mount>                                      integrity check
ipod-read-sysinfo-extended <device> <mount>                write SysInfoExtended (FirewireGuid -> hash72)

The iPod Classic 6G/7G rejects an iTunesDB that is not signed with the correct hash72,
which libgpod derives from the FirewireGuid in iPod_Control/Device/SysInfoExtended.
`ensure_sysinfo_extended()` generates that file from the device before any write.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, Iterator, Optional


class IpodError(RuntimeError):
    pass


@dataclass
class Track:
    """One row of the iTunesDB as reported by gpod-ls -Q."""
    id: int
    ipod_path: str
    title: str
    artist: str
    album: str
    albumartist: str
    genre: str
    tracklen: int          # milliseconds
    size: int              # bytes
    filetype: str

    @property
    def identity(self) -> str:
        """Stable identity for reconciling a source file with an on-iPod track.

        Uses metadata + rounded duration (seconds). Deliberately format-agnostic:
        a FLAC source and its ALAC copy on the iPod share this key.
        """
        # Use the track artist (fallback albumartist). libgpod does not reliably preserve
        # albumartist on copy, but track artist survives on both sides — so it is the
        # stable cross-boundary key. Must match SourceFile.identity.
        return identity_key(
            self.artist or self.albumartist, self.album, self.title, self.tracklen / 1000.0
        )


def identity_key(artist: str, album: str, title: str, seconds: float) -> str:
    def norm(s: str) -> str:
        return " ".join((s or "").strip().lower().split())
    return f"{norm(artist)}\x1f{norm(album)}\x1f{norm(title)}\x1f{round(seconds)}"


def _run(cmd: list[str], *, check: bool = True, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=timeout
    )
    if check and proc.returncode != 0:
        raise IpodError(
            f"command failed ({proc.returncode}): {' '.join(cmd)}\n{proc.stderr[-2000:]}"
        )
    return proc


class Ipod:
    def __init__(self, mount: str, device: Optional[str] = None):
        self.mount = mount
        self.device = device  # raw block device, e.g. /dev/sdg (for SysInfoExtended)

    # --- device preparation -------------------------------------------------
    @property
    def sysinfo_extended_path(self) -> Path:
        return Path(self.mount) / "iPod_Control" / "Device" / "SysInfoExtended"

    def has_sysinfo_extended(self) -> bool:
        p = self.sysinfo_extended_path
        return p.exists() and p.stat().st_size > 0

    def ensure_sysinfo_extended(self) -> bool:
        """Generate SysInfoExtended from the device if missing. Returns True if written."""
        if self.has_sysinfo_extended():
            return False
        if not self.device:
            raise IpodError(
                "SysInfoExtended missing and no raw device given; cannot derive hash72. "
                "Pass the iPod block device (e.g. /dev/sdg)."
            )
        _run(["ipod-read-sysinfo-extended", self.device, self.mount])
        if not self.has_sysinfo_extended():
            raise IpodError("ipod-read-sysinfo-extended ran but SysInfoExtended was not created")
        return True

    # --- reading current state ---------------------------------------------
    def list_tracks(self, timeout: Optional[int] = None) -> list[Track]:
        """Dump the iTunesDB via gpod-ls -Q into a temp SQLite and read it back.

        --disable-checksum avoids probing every file (fast, DB-only).
        """
        with tempfile.TemporaryDirectory() as td:
            db = os.path.join(td, "ipod.sqlite3")
            _run(
                ["gpod-ls", "-M", self.mount, "-Q", db, "--disable-checksum"],
                timeout=timeout,
            )
            return _read_tracks_sqlite(db)

    # --- mutations ----------------------------------------------------------
    def add(
        self,
        files: list[str],
        *,
        encoder: str = "alac",
        threads: Optional[int] = None,
        replace: bool = True,
        on_line: Optional[Callable[[str], None]] = None,
    ) -> None:
        """Add/transcode files into the iTunesDB. gpod-cp transcodes FLAC/WAV,
        copies already-supported formats (mp3/m4a/alac) as-is.

        -c disables gpod-cp's per-file checksum dedup, which otherwise hashes every
        existing on-iPod track (hundreds of GB over USB) on each run. We do our own
        delta (only genuinely-new files are passed here) and -r Y guards duplicates
        by title/album/artist, so the checksum pass is redundant. The device must be
        in gpod_write_supported() (patched to include Classic) — no -F needed."""
        if not files:
            return
        cmd = ["gpod-cp", "-M", self.mount, "-c", "-e", encoder, "-r", "Y" if replace else "N"]
        if threads:
            cmd += ["-T", str(threads)]
        cmd += files
        _stream(cmd, on_line)

    def remove(self, refs: list[str], on_line: Optional[Callable[[str], None]] = None) -> None:
        """Remove tracks by ipod_path or numeric id."""
        if not refs:
            return
        _stream(["gpod-rm", "-M", self.mount, *refs], on_line)

    def verify(self, timeout: Optional[int] = None) -> str:
        return _run(["gpod-verify", "-M", self.mount], check=False, timeout=timeout).stdout


def _read_tracks_sqlite(db_path: str) -> list[Track]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        cols = {r[1] for r in con.execute("PRAGMA table_info(tracks)")}
    except sqlite3.Error as e:
        con.close()
        raise IpodError(f"gpod-ls sqlite has no 'tracks' table: {e}")

    def pick(row, *names, default=""):
        for n in names:
            if n in cols and row[n] is not None:
                return row[n]
        return default

    out: list[Track] = []
    for row in con.execute("SELECT * FROM tracks"):
        out.append(
            Track(
                id=int(pick(row, "id", default=0) or 0),
                ipod_path=str(pick(row, "ipod_path")),
                title=str(pick(row, "title")),
                artist=str(pick(row, "artist")),
                album=str(pick(row, "album")),
                albumartist=str(pick(row, "albumartist", "album_artist")),
                genre=str(pick(row, "genre")),
                tracklen=int(pick(row, "tracklen", "length", default=0) or 0),
                size=int(pick(row, "size", default=0) or 0),
                filetype=str(pick(row, "filetype", "filetype_marker")),
            )
        )
    con.close()
    return out


def _stream(cmd: list[str], on_line: Optional[Callable[[str], None]]) -> None:
    """Run a command, streaming combined output line-by-line to on_line."""
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    )
    assert proc.stdout is not None
    for line in proc.stdout:
        if on_line:
            on_line(line.rstrip("\n"))
    rc = proc.wait()
    if rc != 0:
        raise IpodError(f"command failed ({rc}): {' '.join(cmd)}")


# --- source library scanning ------------------------------------------------

AUDIO_EXTS = {".flac", ".mp3", ".m4a", ".alac", ".aac", ".wav", ".aiff", ".aif"}


@dataclass
class SourceFile:
    path: str
    mtime: float
    size: int
    artist: str
    album: str
    title: str
    seconds: float

    @property
    def identity(self) -> str:
        return identity_key(self.artist, self.album, self.title, self.seconds)


def stat_walk(root: str, exts: Iterable[str] = AUDIO_EXTS) -> Iterator[tuple[str, float, int]]:
    """Fast pass: yield (path, mtime, size) with no tag reading. Lets the sync skip
    the expensive tag read for files already recorded unchanged in the state DB."""
    exts = {e.lower() for e in exts}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            if os.path.splitext(name)[1].lower() not in exts:
                continue
            full = os.path.join(dirpath, name)
            try:
                st = os.stat(full)
            except OSError:
                continue
            yield full, st.st_mtime, st.st_size


def read_source_file(path: str, mtime: float, size: int) -> SourceFile:
    """Read tags for one file (mutagen) and build its SourceFile. Called only for
    new/changed files — not for unchanged ones."""
    from mutagen import File as MutagenFile  # lazy import

    name = os.path.basename(path)
    artist = album = title = ""
    seconds = 0.0
    try:
        mf = MutagenFile(path, easy=True)
        if mf is not None:
            # Track artist (fallback albumartist) — the iPod keeps track artist,
            # not albumartist, so this is the stable key for reconciliation.
            artist = _first(mf, "artist") or _first(mf, "albumartist")
            album = _first(mf, "album")
            title = _first(mf, "title") or os.path.splitext(name)[0]
            if mf.info is not None:
                seconds = float(getattr(mf.info, "length", 0.0) or 0.0)
    except Exception:
        title = os.path.splitext(name)[0]
    return SourceFile(path=path, mtime=mtime, size=size,
                      artist=artist, album=album, title=title, seconds=seconds)


def scan_source(root: str, exts: Iterable[str] = AUDIO_EXTS) -> Iterator[SourceFile]:
    """Walk the source library, reading tags for every file (full scan)."""
    for path, mtime, size in stat_walk(root, exts):
        yield read_source_file(path, mtime, size)


def _first(mf, key: str) -> str:
    try:
        v = mf.get(key)
        if isinstance(v, list) and v:
            return str(v[0])
        return str(v) if v else ""
    except Exception:
        return ""
