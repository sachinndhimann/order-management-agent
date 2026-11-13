"""Logging, metrics, tracing (Chapter 3: observability).

The three pillars and what each answers:
  logs    -> "what happened in THIS request?"   (JSON, one line/event, with request_id)
  metrics -> "how is the system doing overall?" (Prometheus counters/histograms)
  traces  -> "where did the time go across services?" (OpenTelemetry spans)
A correlation id (request_id / trace_id) lives in a ContextVar so every log line
emitted anywhere below the request carries it without passing it around.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from contextvars import ContextVar

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from prometheus_client import Counter, Histogram

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
            "request_id": request_id_var.get(),
        }
        payload.update(getattr(record, "extra_fields", {}))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)


def log_event(logger: logging.Logger, msg: str, **fields) -> None:
    """Structured log helper: log_event(log, "order_created", order_id=...)."""
    logger.info(msg, extra={"extra_fields": fields})


# ---- metrics (low-cardinality labels only! never put user ids / order ids here) ----
HTTP_REQUESTS = Counter("http_requests_total", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram("http_request_seconds", "HTTP latency", ["route"])
ORDERS_CREATED = Counter("orders_created_total", "Orders created", ["result"])
CONNECTOR_CALLS = Counter("connector_calls_total", "Connector calls", ["connector", "outcome"])
MCP_TOOL_CALLS = Counter("mcp_tool_calls_total", "MCP tool calls", ["tool", "outcome"])

# ---- tracing: SDK provider with no exporter by default (zero-dependency). In
# production add OTLPSpanExporter via BatchSpanProcessor; spans start flowing. ----
_provider = TracerProvider()
trace.set_tracer_provider(_provider)
tracer = trace.get_tracer("order-agent")


@contextmanager
def span(name: str, **attrs):
    with tracer.start_as_current_span(name) as s:
        for k, v in attrs.items():
            s.set_attribute(k, v)
        yield s


@contextmanager
def timed(histogram: Histogram, *labels: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        histogram.labels(*labels).observe(time.perf_counter() - start)
