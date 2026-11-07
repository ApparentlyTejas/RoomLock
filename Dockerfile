FROM python:3.12-slim

RUN groupadd --system --gid 1001 app \
    && useradd --system --uid 1001 --gid app --home /code --shell /sbin/nologin app

WORKDIR /code
RUN chown app:app /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY --chown=app:app app ./app
COPY --chown=app:app db ./db
COPY --chown=app:app tests ./tests

EXPOSE 8000

USER app

# Hits the app's own /health route (checks DB connectivity too, not just
# that the process is alive) using python, so the image doesn't need curl
# installed just for this.
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=2).status == 200 else 1)"

# Shell form (not the usual exec-form JSON array) specifically so
# ${WEB_CONCURRENCY:-1} gets expanded — lets docker-compose.scale-demo.yml
# run multiple worker processes per container for real throughput under
# load, while every other deployment (including Render) keeps the safe
# single-worker default without needing its own separate Dockerfile.
CMD uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-1}
