# iPodSync

Sync an Unraid music library directly to a USB-connected iPod Classic — no Mac, no
iTunes. Transcodes FLAC to ALAC (MP3/M4A copied as-is), writes the iTunesDB on the
device, and syncs only the delta. Web UI to configure, run, and monitor.

Built on [gpod-utils](https://github.com/whatdoineed2do/gpod-utils) / libgpod.

## How it works

- **Source of truth**: the main library at `/mnt/user/Music` (untouched, read-only).
- **Transcode + DB write**: `gpod-cp` transcodes FLAC→ALAC via ffmpeg and writes the
  hash72-signed iTunesDB the iPod Classic firmware requires.
- **Incremental**: `gpod-ls -Q` dumps the current iTunesDB to SQLite; the engine diffs
  it against the library and copies/removes only what changed. State lives in
  `/config/state.sqlite`.
- **First run**: no state yet, so existing iPod tracks are reconciled by identity
  (artist/album/title/duration) and kept without re-copying.

### iPod Classic support (the catch)

gpod-utils gates iPod Classic behind `-F` and its `gpod-rm` refuses "unsupported"
devices outright, because these models need a hash72-signed DB. The Dockerfile patches
`gpod_write_supported()` to whitelist `CLASSIC_1/2/3`, so both copy and remove work
natively. hash72 needs the FireWireGUID from `iPod_Control/Device/SysInfoExtended`;
the engine generates it from the raw device (`ipod-read-sysinfo-extended`) on first run.

## Prerequisites

- iPod formatted **FAT32** (Windows/"PC" firmware). Mac/HFS+ needs a one-time reformat.
- Unraid **Unassigned Devices** plugin to see the disk. **Disable UD automount for the
  iPod** — the container mounts it itself (so it can eject cleanly). One owner only.
- The iPod is auto-detected by its FAT **label** (default `IPOD`), so there's no device
  node to configure.

## Mount model

The container mounts the iPod partition at `/ipod` itself and unmounts + SCSI-ejects it
on **Eject**, so no host-namespace tricks are needed. It finds the iPod by FAT **label**
(`IPOD`) at mount time via `/dev/disk/by-label`, so:

- the container **starts with no iPod attached** (nothing is pinned at start), and
- a changed `/dev/sdX` across replugs just works.

This needs `CAP_SYS_ADMIN` + `seccomp=unconfined` (Docker's default seccomp blocks the
`mount` syscall), plus `/dev` bind-mounted and a block-device cgroup rule so hot-plugged
devices are visible and accessible.

## Build & run (Unraid)

```sh
# build the image on the server
cd /mnt/user/appdata/ipodsync/src && docker build -t ipodsync:local .

# run (no device pinning — the iPod is found by label; UD automount for the iPod must be OFF)
docker run -d --name ipodsync --restart unless-stopped -p 8580:8580 \
  --cap-add SYS_RAWIO --cap-add SYS_ADMIN \
  --security-opt seccomp=unconfined --security-opt apparmor=unconfined \
  --device-cgroup-rule='b *:* rmw' -v /dev:/dev \
  -v /mnt/user/Music:/music:ro \
  -v /mnt/user/appdata/ipodsync/config:/config \
  ipodsync:local
```

Or add the container via `unraid-template.xml`. Then open `http://<server>:8580/`.

The web UI: **Mount/Eject** control the iPod filesystem; **Dry run** previews add/remove
counts; **Sync now** runs it with a live log. Toggle **Prune** for full-mirror (remove
tracks no longer in the library) vs additive-only. The iPod auto-mounts when the
container starts.

## CLI (no web UI)

```sh
docker exec ipodsync python3 -m engine.sync --dry-run     # or: --no-prune, or plain sync
```

## Safety / recovery

- The original iTunesDB + `Device/` are backed up under
  `/mnt/user/appdata/ipodsync/ipod-backup-<timestamp>/`. To roll back, copy the backed-up
  `iTunes/iTunesDB` back to `iPod_Control/iTunes/` on the iPod.
- Always let a sync finish, then click **Eject** in the web UI (flush + unmount) before
  unplugging.

## Notes / caveats

- The iPod is found by FAT label (default `IPOD`); no device node is pinned, so replugs
  and node changes are handled automatically. Set a different label in Settings if needed.
- After replugging, the iPod auto-mounts if the container (re)starts, or click **Mount**.
- Reported capacity reflects what the firmware exposes (often ~1 TB on modded units), not raw SSD size.
- Identity matching is deliberately strict to avoid false matches; a first sync after a
  library re-tag may show larger add/remove counts, then converges.
