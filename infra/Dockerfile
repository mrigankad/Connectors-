FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY catalog ./catalog
RUN pip install --no-cache-dir uv && uv pip install --system --no-cache . && rm -rf /root/.cache
USER 1000
ENTRYPOINT []
CMD ["wayam-connectors", "serve", "--host", "0.0.0.0", "--port", "8090"]
