FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY data ./data
COPY run.sh ./
RUN useradd -m -u 10001 pm && mkdir -p /app/var /app/artifacts && chown -R pm /app/var /app/artifacts
USER pm
ENV DB_PATH=/app/var/pm_assistant.db ARTIFACTS_DIR=/app/artifacts APP_PORT=8091 PYTHON_BIN=python
EXPOSE 8091
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8091/health')" || exit 1
CMD ["./run.sh"]
