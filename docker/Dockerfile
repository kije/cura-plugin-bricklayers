FROM lscr.io/linuxserver/cura:latest

RUN apt-get update -qq \
    && apt-get install -y --no-install-recommends x11vnc \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Fix: Rosetta CDI creates /config/.cache as root before s6 init-adduser runs.
# Cura needs to write containers.db into the cache dir.
RUN printf '#!/bin/bash\nchown -R abc:abc /config/.cache 2>/dev/null || true\nmkdir -p /config/.cache/cura/5.12\nchown -R abc:abc /config/.cache/cura\n' \
      > /etc/s6-overlay/s6-rc.d/init-config/fix-cache-perms \
    && chmod +x /etc/s6-overlay/s6-rc.d/init-config/fix-cache-perms \
    || true

# Also add as a custom-init script (linuxserver pattern)
RUN mkdir -p /custom-cont-init.d \
    && printf '#!/bin/bash\nchown -R abc:abc /config/.cache 2>/dev/null || true\nmkdir -p /config/.cache/cura/5.12\nchown -R abc:abc /config/.cache/cura\n' \
       > /custom-cont-init.d/fix-cache-perms.sh \
    && chmod +x /custom-cont-init.d/fix-cache-perms.sh

# s6 service: start x11vnc on port 5900, sharing the existing X display
RUN mkdir -p /etc/s6-overlay/s6-rc.d/x11vnc/dependencies.d \
    && echo "longrun" > /etc/s6-overlay/s6-rc.d/x11vnc/type \
    && printf '#!/bin/bash\n\n# Wait for X server to be ready\nwhile ! xdpyinfo -display :1 >/dev/null 2>&1; do sleep 1; done\nexec x11vnc -display :1 -forever -shared -nopw -rfbport 5900 -noxdamage -noshm\n' \
       > /etc/s6-overlay/s6-rc.d/x11vnc/run \
    && chmod +x /etc/s6-overlay/s6-rc.d/x11vnc/run \
    && touch /etc/s6-overlay/s6-rc.d/x11vnc/dependencies.d/svc-xorg \
    && touch /etc/s6-overlay/s6-rc.d/user/contents.d/x11vnc
