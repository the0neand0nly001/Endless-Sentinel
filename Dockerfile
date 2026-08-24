FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FLASK_HOST=0.0.0.0 \
    FLASK_PORT=8080 \
    ENABLE_BACKGROUND_POLLER=true

WORKDIR /app

RUN groupadd --gid 10001 sentinel \
    && useradd --uid 10001 --gid sentinel --create-home --shell /usr/sbin/nologin sentinel

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY app.py ./
COPY modules ./modules
COPY templates ./templates
COPY static ./static

RUN chown -R sentinel:sentinel /app
USER sentinel

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=3)" || exit 1

CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "120", "--access-logfile", "-", "--error-logfile", "-", "app:app"]
