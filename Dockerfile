# Built by the GitHub Action (.github/workflows/build-image.yml) and published to
# GHCR. Deploy with compose.yaml - there is no local-build deploy path.
FROM python:3.12-slim

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/*.py ./
# static/ includes static/fonts/ - any font files committed there are baked in
COPY app/static ./static
# the newest "## x.y" heading is the app's version; also shown in-app
COPY CHANGELOG.md ./

# /data holds everything that must survive image updates: ipam.db, backups/,
# and optional drop-in fonts/. Mount it as a volume.
ENV IPAM_DB=/data/ipam.db
ENV PORT=20080
VOLUME ["/data"]
EXPOSE 20080

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/healthz' % os.environ.get('PORT','20080'), timeout=4)" || exit 1

CMD ["python", "app.py"]
