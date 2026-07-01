# Build context is the repo root; app code lives under app/
FROM python:3.12-slim

WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/app.py .
COPY app/static ./static

ENV IPAM_DB=/data/ipam.db
ENV PORT=20080
EXPOSE 20080

CMD ["python", "app.py"]
