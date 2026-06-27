"""
Query audit decorator.

When `Config.AUDIT_QUERIES` is truthy, a row is inserted into `query_audit`
per request hitting a decorated endpoint. The insert is best-effort —
failures are swallowed so audit machinery can never break the request path.

Default-off in production; flip via `AUDIT_QUERIES=true` env var, no redeploy
needed (the decorator reads Config on every call).
"""
import json
import logging
import time
from functools import wraps

from flask import request, session

from app.database import get_conn
from config import Config

log = logging.getLogger(__name__)

# Param keys that should never land in the audit table even if present in the
# query string or JSON body — defence-in-depth, the decorated endpoints don't
# accept these as args today but a future addition shouldn't leak them.
_SCRUB_KEYS = {"password", "new_password", "old_password", "token", "secret", "csrf"}


def _scrub(d):
    if not isinstance(d, dict):
        return d
    return {k: ("***" if k.lower() in _SCRUB_KEYS else v) for k, v in d.items()}


def _client_ip():
    fwd = request.headers.get("X-Forwarded-For", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _row_count(payload):
    """Best-effort count for the response body — list length, or .get('rows'/'teams')
    if the body wraps results in a known field. Returns None when shape is unknown."""
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        for k in ("rows", "teams", "members", "results", "items", "data", "breakdown"):
            v = payload.get(k)
            if isinstance(v, list):
                return len(v)
    return None


def audit_query(fn):
    """Decorator: log this endpoint's request to query_audit when enabled."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not Config.AUDIT_QUERIES:
            return fn(*args, **kwargs)

        t0 = time.perf_counter()
        rv = fn(*args, **kwargs)
        duration_ms = int((time.perf_counter() - t0) * 1000)

        try:
            # Flask view return shapes: Response | (body, status) | (body, status, headers)
            response = rv[0] if isinstance(rv, tuple) else rv
            status_code = getattr(response, "status_code", 200)
            try:
                body = response.get_json(silent=True) if hasattr(response, "get_json") else None
            except Exception:
                body = None
            n_rows = _row_count(body)

            params = dict(request.args.to_dict(flat=True))
            params = _scrub(params)

            conn = get_conn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        INSERT INTO query_audit
                          (user_id, role, endpoint, method, params, ip,
                           row_count, duration_ms, status_code)
                        VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                        """,
                        (
                            session.get("user_id"),
                            session.get("role"),
                            request.path[:120],
                            request.method,
                            json.dumps(params, default=str),
                            _client_ip()[:64],
                            n_rows,
                            duration_ms,
                            status_code,
                        ),
                    )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:  # pragma: no cover
            # Never block the request on audit failures.
            log.warning("audit_query insert failed (swallowed): %s", e)

        return rv

    return wrapper
