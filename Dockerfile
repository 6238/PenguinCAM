# Debian 13 (trixie, glibc 2.41): Orca Slicer's Ubuntu 24.04 AppImage needs
# glibc 2.38 or newer. Pinned so a future tag move cannot change the C library.
FROM python:3.11-slim-trixie

WORKDIR /app

# curl fetches the Orca release; the rest are the runtime libraries the Orca
# binary links against (resolved from the trixie package index, identical on
# amd64 and arm64). The AppImage bundles only a few private libraries.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl \
        libopengl0 libegl1 libgl1 libglx0 libglu1-mesa \
        libgtk-3-0t64 libgdk-pixbuf-2.0-0 libatk1.0-0t64 libcairo2 libcairo-gobject2 \
        libpango-1.0-0 libpangocairo-1.0-0 libpangoft2-1.0-0 libharfbuzz0b libfontconfig1 \
        libglib2.0-0t64 libdbus-1-3 \
        libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 \
        libwebkit2gtk-4.1-0 libjavascriptcoregtk-4.1-0 libsoup-3.0-0 libsecret-1-0 \
        libwayland-client0 libwayland-egl1 libwayland-server0 libxkbcommon0 \
        libx11-6 libxext6 libsm6 libice6 \
        libstdc++6 libexpat1 liblzma5 libmspack0t64 zlib1g \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Orca Slicer at the fixed path tools/orca (the version is pinned in the script).
# Done before copying the app so code changes do not re-download 130 MB.
COPY scripts/install-orca.sh scripts/install-orca.sh
RUN scripts/install-orca.sh

# Copy application code (tools/ is in .dockerignore, so the install above stays)
COPY . .

# The build fails if the Orca binary has an unresolved library.
RUN test -x /app/tools/orca/squashfs-root/bin/orca-slicer \
    && ! (LD_LIBRARY_PATH=/app/tools/orca/squashfs-root/lib/orca-runtime:/app/tools/orca/squashfs-root/bin \
        ldd /app/tools/orca/squashfs-root/bin/orca-slicer | grep 'not found')

# Expose port (Railway sets PORT env var)
EXPOSE $PORT

# One worker: the job table, the file token map and the limiter storage are per
# process, so the worker count must stay at one (gunicorn otherwise reads WEB_CONCURRENCY).
# Threaded so open event streams do not block other requests. Keep Procfile in step.
CMD gunicorn frc_cam_gui_app:app --bind 0.0.0.0:$PORT --workers 1 --worker-class gthread --threads 16
