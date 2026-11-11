FROM debian:bookworm-slim
ENV DEBIAN_FRONTEND=noninteractive WINEPREFIX=/opt/wine WINEARCH=win64 WINEDEBUG=-all DISPLAY=:99 PYTHONUNBUFFERED=1
RUN dpkg --add-architecture i386 && apt-get update && apt-get install -y --no-install-recommends ca-certificates curl wget gnupg2 xvfb fluxbox x11vnc firefox-esr cabextract winbind procps sqlite3 tzdata python3 python3-pip && curl -fsSL https://raw.githubusercontent.com/Winetricks/winetricks/master/src/winetricks -o /usr/local/bin/winetricks && chmod 755 /usr/local/bin/winetricks && mkdir -p /etc/apt/keyrings && curl -fsSL https://dl.winehq.org/wine-builds/winehq.key -o /etc/apt/keyrings/winehq.key && echo 'deb [signed-by=/etc/apt/keyrings/winehq.key] https://dl.winehq.org/wine-builds/debian/ bookworm main' > /etc/apt/sources.list.d/winehq.list && apt-get update && apt-get install -y --install-recommends winehq-stable && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/wine /opt/tradingbot && curl -fsSL https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe -o /opt/python.exe
COPY deploy/linux/mt5/entrypoint.sh /usr/local/bin/tradingbot-entrypoint
COPY deploy/linux/mt5/healthcheck.sh /usr/local/bin/tradingbot-healthcheck
RUN chmod 755 /usr/local/bin/tradingbot-entrypoint /usr/local/bin/tradingbot-healthcheck
WORKDIR /opt/tradingbot
EXPOSE 5900
ENTRYPOINT ["/usr/local/bin/tradingbot-entrypoint"]
