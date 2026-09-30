# DownloadWatch for Plex. Standard-library Python on Alpine; runs as an unprivileged user.
FROM python:3.13-alpine@sha256:79e7a9b9ff1cbceff819f856fb374477792a5967759d94df266de7b7b4120e6f

# tzdata: dashboard and CSV times in your TIMEZONE
RUN apk add --no-cache tzdata \
 && addgroup -g 1000 downloadwatch \
 && adduser -D -H -u 1000 -G downloadwatch downloadwatch \
 && mkdir -p /data && chown 1000:1000 /data

COPY --chown=root:root app/ /app/

ENV DATA_DIR=/data \
    PORT=8090 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER 1000:1000
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=4).status == 200 else 1)" || exit 1

LABEL org.opencontainers.image.title="DownloadWatch for Plex" \
      org.opencontainers.image.description="Permanent history of who downloaded what from your Plex server" \
      org.opencontainers.image.licenses="MIT"

CMD ["python", "/app/downloadwatch.py"]
