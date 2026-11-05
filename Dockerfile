FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RADAR_DATA_PATH=/data \
    RADAR_HOST=0.0.0.0 \
    RADAR_PORT=8001 \
    RADAR_BASE_PATH=/v2

RUN groupadd --system --gid 1000 app \
    && useradd --system --uid 1000 --gid app --home-dir /app app

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

ARG GIT_SHA=dev
ENV GIT_SHA=${GIT_SHA}

RUN mkdir -p /data && chown app:app /data
USER app
VOLUME ["/data"]
EXPOSE 8001

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8001/v2/health', timeout=4).status == 200 else 1)"

CMD ["python", "-m", "radar", "serve"]
