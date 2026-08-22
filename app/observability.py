"""
Observability: a Prometheus-format /metrics endpoint (request counts,
latency histograms, in-progress requests — via
prometheus-fastapi-instrumentator, which wraps every route automatically)
plus structured JSON access logs on every request.

Neither of these exists to feed a dashboard nobody's watching. They exist
because the load tests need an authoritative source for latency/error-rate
numbers instead of eyeballing a load generator's own summary — /metrics is
what loadtest/report.py scrapes to compute p50/p95/p99 from the server's
own view of each request, which is the number that actually matters (a
load generator's client-side timing includes its own scheduling jitter).
"""

import json
import logging
import time

from fastapi import FastAPI, Request
from prometheus_fastapi_instrumentator import Instrumentator
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

access_logger = logging.getLogger("roomlock.access")
access_logger.setLevel(logging.INFO)
if not access_logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(message)s"))
    access_logger.addHandler(_handler)
access_logger.propagate = False


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers["X-Replica-Id"] = settings.replica_id
        access_logger.info(
            json.dumps(
                {
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(duration_ms, 2),
                    "replica_id": settings.replica_id,
                }
            )
        )
        return response


def setup_observability(app: FastAPI) -> None:
    app.add_middleware(AccessLogMiddleware)
    Instrumentator().instrument(app).expose(app, include_in_schema=False)
