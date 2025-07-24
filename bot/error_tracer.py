import logging
import os

try:
    import sentry_sdk
    _SENTRY_AVAILABLE = True
except Exception:  # pragma: no cover
    _SENTRY_AVAILABLE = False

_sentry_inited = False

def init_error_tracer():
    global _sentry_inited
    dsn = os.getenv("SENTRY_DSN")
    if dsn and _SENTRY_AVAILABLE:
        sentry_sdk.init(dsn=dsn, traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "0.0")))
        _sentry_inited = True
        logging.info("Sentry initialized")
    else:
        logging.info("Sentry not configured or not installed")

def capture_exception(exc):
    if _sentry_inited:
        sentry_sdk.capture_exception(exc)
    else:
        logging.exception(exc)
