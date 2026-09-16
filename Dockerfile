FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sentinel/ ./sentinel/
COPY run.py config.yaml ./

# data/ and logs/ are bind-mounted or volume-backed so state survives restarts.
RUN mkdir -p data logs && \
    useradd --create-home --uid 10001 sentinel && \
    chown -R sentinel:sentinel /app
USER sentinel

# Fails the container health check if the poller has stopped writing.
HEALTHCHECK --interval=120s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import sys,time,pathlib; \
p=pathlib.Path('logs/sentinel.log'); \
sys.exit(0 if p.exists() and time.time()-p.stat().st_mtime < 600 else 1)"

CMD ["python", "run.py", "run"]
