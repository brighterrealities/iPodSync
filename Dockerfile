# iPodSync — direct iPod Classic sync for Unraid
# Builds gpod-utils (libgpod 0.8.3) + ffmpeg, runs the sync engine + FastAPI web UI.
# trixie, not bookworm: gpod-utils needs ffmpeg 7 (libavcodec 61) — its pre-7 compat
# shim for the FF_PROFILE_*/AV_PROFILE_* rename is a no-op, so it will not build against
# bookworm's ffmpeg 5. trixie also carries libgpod 0.8.3, so nothing else changes.
FROM debian:trixie-slim AS build

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential autoconf automake libtool pkg-config git ca-certificates \
        libglib2.0-dev libgpod-dev libjson-c-dev libsqlite3-dev \
        libavformat-dev libavcodec-dev libavutil-dev libswresample-dev libswscale-dev \
    && rm -rf /var/lib/apt/lists/*

# gpod-utils: gpod-cp / gpod-ls / gpod-rm / gpod-verify / gpod-playlist / gpod-tag.
# We build the d3vil-st fork, not whatdoineed2do upstream. It carries the iPod Classic
# whitelist we used to apply by hand (identical patch to gpod_write_supported()'s
# `supported[]`), plus fixes upstream lacks and we need:
#   - caps ALAC output at 48 kHz (upstream writes 96/176/192 kHz ALAC the Classic can't decode)
#   - writes albumartist / compilation / sort tags (upstream silently drops them)
#   - embeds cover art in transcoded m4a; preserves disc + total-track numbering
#   - adds gpod-playlist (playlist CRUD + iTunes-compatible smart playlists)
ARG GPOD_UTILS_REPO=https://github.com/d3vil-st/gpod-utils.git
ARG GPOD_UTILS_REF=master
RUN git clone --depth 1 --branch "${GPOD_UTILS_REF}" "${GPOD_UTILS_REPO}" /src/gpod-utils \
    && cd /src/gpod-utils \
    && grep -n "ITDB_IPOD_GENERATION_CLASSIC" src/lib/gpod-utils.c \
    && autoreconf --install \
    && ./configure --prefix=/usr/local \
    && make -j"$(nproc)" \
    && make install DESTDIR=/out \
    && make install

FROM debian:trixie-slim

# Runtime libs (match the -dev libs above), ffmpeg CLI, python for the engine + web UI.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libglib2.0-0t64 libgpod4t64 libgpod-common libjson-c5 libsqlite3-0 \
        libavformat61 libavcodec61 libavutil59 libswresample5 libswscale8 \
        ffmpeg \
        python3 python3-pip \
        dosfstools mtools udev eject sg3-utils librsvg2-bin \
    && rm -rf /var/lib/apt/lists/*
# libgpod-common ships ipod-read-sysinfo-extended (reads FirewireGuid -> SysInfoExtended over SCSI),
# required for hash72 so the iPod Classic accepts the DB we write.

# gpod-utils binaries + libgpod runtime data from the build stage
COPY --from=build /out/ /
RUN ldconfig

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir --break-system-packages -r requirements.txt

COPY engine /app/engine
COPY webapp /app/webapp

# Rasterize the iPod icon to PNG for iOS home-screen / Unraid (both prefer PNG).
# 180px is the exact iOS apple-touch-icon size; 256px for Unraid/manifest.
RUN rsvg-convert -w 256 -h 256 /app/webapp/static/icon.svg -o /app/webapp/static/icon.png \
 && rsvg-convert -w 180 -h 180 /app/webapp/static/icon.svg -o /app/webapp/static/icon-180.png

ENV IPODSYNC_MUSIC=/music \
    IPODSYNC_IPOD=/ipod \
    IPODSYNC_CONFIG=/config \
    IPODSYNC_PORT=8580

RUN mkdir -p /ipod          # container-owned mountpoint for the iPod partition

# Unraid dashboard icon (host-independent). The WebUI link comes from the template's
# <WebUI> tag (auto-fills the server IP), so no host is hardcoded here.
LABEL net.unraid.docker.icon="https://raw.githubusercontent.com/brighterrealities/iPodSync/main/webapp/static/icon.png"

EXPOSE 8580
VOLUME ["/config"]

# Default: web UI. Override CMD to run the CLI engine directly (python3 -m engine.sync ...).
CMD ["python3", "-m", "uvicorn", "webapp.app:app", "--host", "0.0.0.0", "--port", "8580"]
