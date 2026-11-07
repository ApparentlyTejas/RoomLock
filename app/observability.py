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
import uuid

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
        # Reuse a client-supplied X-Request-ID if present (lets a client
        # correlate its own logs with this request across a proxy/load
        # balancer); otherwise mint one. Stored on request.state so any
        # route handler can also log it.
        request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
        request.state.request_id = request_id
        start = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            response.headers["X-Replica-Id"] = settings.replica_id
            response.headers["X-Request-ID"] = request_id
            return response
        finally:
            # finally, not just the success path: an unhandled exception
            # propagates straight through call_next with nothing below
            # this middleware to log it, which previously meant a 500
            # produced zero access-log output at all.
            duration_ms = (time.perf_counter() - start) * 1000
            access_logger.info(
                json.dumps(
                    {
                        "request_id": request_id,
                        "method": request.method,
                        "path": request.url.path,
                        "status": status_code,
                        "duration_ms": round(duration_ms, 2),
                        "replica_id": settings.replica_id,
                    }
                )
            )


def setup_observability(app: FastAPI) -> None:
    app.add_middleware(AccessLogMiddleware)
    Instrumentator().instrument(app).expose(app, include_in_schema=False)
