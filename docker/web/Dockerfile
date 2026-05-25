FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    sqlite3 \
  && rm -rf /var/lib/apt/lists/*

COPY app/web/requirements.txt /tmp/req.txt
RUN pip install --no-cache-dir -r /tmp/req.txt

# /app is mounted via compose so source changes are reflected without rebuild
EXPOSE 8081

CMD ["gunicorn", "--chdir", "/app", "--bind", "0.0.0.0:8081", \
     "--workers", "1", "--threads", "4", "--timeout", "600", \
     "--access-logfile", "-", "--error-logfile", "-", \
     "web.app:app"]
