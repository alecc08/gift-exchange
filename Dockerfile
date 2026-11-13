FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DB_PATH=/app/data/exchange.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py ./
COPY static/ ./static/

EXPOSE 8000

# --no-access-log: uvicorn's own access lines log the *socket* address, which behind
# Traefik is the proxy. main.py logs one line per request with the X-Forwarded-For
# address, the status and the latency, so that is kept as the single record.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
