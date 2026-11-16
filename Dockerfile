# Production image:  docker build --target prod -t a2a-bridge .
# Demo image (adds the mock Boomi listener and Execute API; used by docker-compose.yml):  docker build -t a2a-bridge-demo .
FROM python:3.14-slim AS prod
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 A2A_BRIDGE_LOG_FORMAT=json
WORKDIR /app
COPY requirements.lock ./
RUN pip install -r requirements.lock
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-deps .
RUN useradd --create-home --uid 10001 app && mkdir /data && chown app /data
USER app
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/readyz', timeout=3)"
CMD ["a2a-bridge", "--config", "/etc/a2a-bridge/agents.yaml"]

FROM prod AS demo
COPY --chown=app tests/mocks ./mocks
COPY --chown=app agents.mock.yaml ./
CMD ["a2a-bridge", "--config", "agents.mock.yaml"]
