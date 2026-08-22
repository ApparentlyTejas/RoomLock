FROM python:3.12-slim

WORKDIR /code

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY db ./db
COPY tests ./tests

EXPOSE 8000

# Shell form (not the usual exec-form JSON array) specifically so
# ${WEB_CONCURRENCY:-1} gets expanded — lets docker-compose.scale-demo.yml
# run multiple worker processes per container for real throughput under
# load, while every other deployment (including Render) keeps the safe
# single-worker default without needing its own separate Dockerfile.
CMD uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY:-1}
