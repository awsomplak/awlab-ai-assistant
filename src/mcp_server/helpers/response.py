"""
Response helpers — standardized success/error response objects and JSON serialization.

All tool handlers should use ``ok_obj`` / ``fail_obj`` for dict returns or
``ok_json`` / ``fail_json`` for string returns (legacy).
"""

import datetime
import json
from typing import Any

from .logger import logger

# ── Response Maker ─────────────────────────────────────────────────────────


def resp_obj(**attrs: Any) -> dict:
    """Build a plain response dict from keyword arguments."""
    payload: dict[str, Any] = {}
    payload.update(attrs)
    return payload


def resp_json(**attrs: Any) -> str:
    """Build a JSON response string from keyword arguments."""
    payload = resp_obj(**attrs)
    return _to_json(payload)


def ok_obj(**attrs: Any) -> dict:
    """Build a success response dict with ``success: True`` plus optional fields."""
    payload = resp_obj(**attrs)
    result = {"success": True} | payload
    return result


def ok_json(**attrs: Any) -> str:
    """Build a success JSON string."""
    payload = ok_obj(**attrs)
    return _to_json(payload)


def fail_obj(error: str, **attrs: Any) -> dict:
    """Build a failure response dict with ``success: False`` and error message."""
    payload: dict[str, Any] = {"success": False, "error": error}
    payload.update(attrs)
    logger.warning(error)
    return payload


def fail_json(error: str, **attrs: Any) -> str:
    """Build a failure JSON string."""
    payload = fail_obj(error, **attrs)
    return _to_json(payload)


# ── Response Parser ────────────────────────────────────────────────────────


def validate_resp(response: str | dict | None = None) -> bool:
    """Validate that a response dict has ``success: True``."""

    try:
        resp = json.loads(response) if isinstance(response, str) else response
        return bool(resp.get("success")) if isinstance(resp, dict) else False
    except (json.JSONDecodeError, TypeError):
        return False


# ── Helper ─────────────────────────────────────────────────────────────────


def _to_json(payload: dict[str, Any]) -> str:
    try:
        return json.dumps(payload, indent=2, default=_json_converter)
    except (TypeError, ValueError) as e:
        return json.dumps({"success": False, "error": f"Serialization failed: {str(e)}"}, indent=2)


def _json_converter(obj):
    if isinstance(obj, (datetime.datetime, datetime.date)):
        return obj.isoformat()  # Converts datetime to "2026-06-28T11:43:47..."
    if isinstance(obj, set):
        return list(obj)  # Converts sets to arrays
    # return str(obj)             # Fallback string representation
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
