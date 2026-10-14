FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    HEADLESS=true
WORKDIR /app

# Dependências e Chromium em camada própria: só reconstrói quando
# requirements.txt muda, não a cada alteração no código.
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && python -m playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && useradd --create-home robot

COPY pyproject.toml ./
COPY app ./app
RUN pip install --no-deps . \
    && mkdir -p /app/outputs && chown robot:robot /app/outputs

USER robot
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
    CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8000\")}/health', timeout=5)"
# PORT permite que plataformas como o Render escolham a porta.
CMD ["sh", "-c", "exec uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000}"]
