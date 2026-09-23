# Build context is the repo root; app code lives under app/
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

CMD ["python", "app.py"]
