FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
COPY tests/mocks ./mocks
COPY agents.mock.yaml ./
RUN useradd --create-home app && mkdir /data && chown app /data
USER app
EXPOSE 8000
CMD ["a2a-bridge", "--config", "agents.mock.yaml"]
