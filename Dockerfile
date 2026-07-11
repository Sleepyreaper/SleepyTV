# SleepyTV — zero-dependency Python Live-TV backend (M3U + XMLTV + HLS proxy)
# plus the optional browser UI (kept for Opera-VPN geo-hopping).
FROM python:3.12-slim

WORKDIR /app
COPY app.py index.html ./

# Port and cache location are configurable; cache lives on a mounted volume so
# the channel list / EPG survive container restarts.
ENV SLEEPYTV_PORT=8080 \
    SLEEPYTV_CACHE_DIR=/cache
EXPOSE 8080
VOLUME ["/cache"]

# No pip install — the app is pure standard library.
CMD ["python", "app.py"]
