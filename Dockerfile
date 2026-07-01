FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

ENV IPAM_DB=/data/ipam.db
ENV PORT=20080
EXPOSE 20080

CMD ["python", "app.py"]
