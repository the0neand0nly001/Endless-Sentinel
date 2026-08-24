FROM python:3.13-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN groupadd --system --gid 10001 sentinel && useradd --system --uid 10001 --gid sentinel --home-dir /app sentinel
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY --chown=sentinel:sentinel . .
USER sentinel
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=4s --start-period=15s --retries=3 CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3)"
CMD ["gunicorn","--workers","1","--threads","8","--bind","0.0.0.0:8080","--access-logfile","-","--error-logfile","-","app:app"]
