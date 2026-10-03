"""Safe generic webhook delivery for the ``notify.webhook`` pipeline step.

The contract is exactly one HTTPS POST of an explicitly configured JSON payload:

- the destination URL and the full ``Authorization`` header value are read only
  from the environment variables named by ``url_env`` and ``authorization_env``;
- redirects are never followed and there are no automatic retries;
- only ``2xx`` responses count as successful;
- the response body is never read;
- the destination URL, the authorization value, the payload and raw transport
  exceptions never appear in messages or saved logs: failures are reported as a
  safe category plus the HTTP status when the server answered.
"""

from __future__ import annotations

import json
import math
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

import typer

from ..redaction import SecretRedactor

DEFAULT_WEBHOOK_TIMEOUT_SECONDS = 30.0

# Plain environment variable names only: no interpolation, whitespace or
# punctuation. This also rejects ``${...}`` inside an env key name itself.
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class WebhookError(Exception):
    """Safe webhook delivery failure.

    ``category`` is a stable, credential-free identifier (``network_error``,
    ``timeout``, ``ssl_error`` or ``http_error``) and ``status`` carries the
    HTTP status code when the server answered. The URL, the authorization value
    and transport details never become part of the message.
    """

    def __init__(self, category: str, status: int | None = None):
        super().__init__(category, status)
        self.category = category
        self.status = status

    def __str__(self) -> str:
        if self.status is not None:
            return f"{self.category} (HTTP {self.status})"
        return self.category


def validate_env_key_name(value: Any, label: str) -> str:
    """Validate a plain environment variable name (``url_env``/``authorization_env``)."""
    if not isinstance(value, str) or not value.strip():
        raise typer.BadParameter(f"notify.webhook {label} must be a non-empty environment variable name")
    name = value.strip()
    if ENV_KEY_RE.fullmatch(name) is None:
        raise typer.BadParameter(
            f"notify.webhook {label} must be a plain environment variable name like MY_WEBHOOK_URL "
            "(no interpolation, whitespace or punctuation)"
        )
    return name


def validate_payload_object(payload: Any) -> dict[str, Any]:
    """Require a non-empty JSON object; the whole context is never sent."""
    if not isinstance(payload, dict):
        raise typer.BadParameter("notify.webhook payload must be a JSON object")
    if not payload:
        raise typer.BadParameter("notify.webhook payload must not be empty")
    return payload


def validate_timeout_seconds(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise typer.BadParameter("notify.webhook timeout_seconds must be a positive finite number of seconds")
    if not math.isfinite(value) or value <= 0:
        raise typer.BadParameter("notify.webhook timeout_seconds must be a positive finite number of seconds")
    return float(value)


def validate_fail_on_error(value: Any) -> bool:
    if not isinstance(value, bool):
        raise typer.BadParameter("notify.webhook fail_on_error must be a boolean")
    return value


def validate_destination_url(url: str) -> None:
    """Accept only HTTPS URLs with a host, without userinfo or fragment."""
    problems: list[str] = []
    try:
        parts = urllib.parse.urlsplit(url)
        parts.port  # noqa: B018 - raises ValueError for invalid ports
    except ValueError as exc:
        raise typer.BadParameter("notify.webhook destination must be a valid HTTPS URL") from exc
    if parts.scheme.lower() != "https":
        problems.append("only the https scheme is allowed")
    if not parts.hostname:
        problems.append("a host is required")
    if parts.username or parts.password:
        problems.append("userinfo is not allowed")
    if parts.fragment:
        problems.append("a fragment is not allowed")
    if problems:
        raise typer.BadParameter(
            "notify.webhook destination must be an HTTPS URL with a host and without userinfo or "
            "fragment: " + "; ".join(problems)
        )


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Redirects are never followed: a 3xx response surfaces as an HTTPError."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_webhook_response(request: urllib.request.Request, timeout: float):
    """Open exactly one response with verified TLS and redirects disabled."""
    opener = urllib.request.build_opener(
        _NoRedirectHandler(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )
    return opener.open(request, timeout=timeout)


def _payload_secret_categories(env: Mapping[str, str], url: str, authorization: str, body: bytes) -> list[str]:
    """Return safe categories of known secrets found verbatim inside the payload."""
    text = body.decode("utf-8", errors="replace")
    categories: list[str] = []
    if url in text:
        categories.append("destination URL")
    if authorization and authorization in text:
        categories.append("authorization value")
    if SecretRedactor.from_env(env).find_secrets(text):
        categories.append("known context secret")
    return categories


def _transport_category(exc: BaseException) -> str:
    """Map a transport failure to a safe, credential-free category."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "ssl_error"
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BaseException) and reason is not exc:
            return _transport_category(reason)
        return "network_error"
    return "network_error"


def send_webhook(
    env: Mapping[str, str],
    *,
    url_env: str,
    payload: dict[str, Any],
    authorization_env: str | None = None,
    timeout_seconds: float = DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
) -> int:
    """Perform exactly one HTTPS POST of the explicit JSON payload.

    Returns the successful 2xx HTTP status. Configuration problems raise
    ``typer.BadParameter``; delivery failures raise the safe
    :class:`WebhookError`. Nothing is retried and redirects are never followed.
    """
    url_key = validate_env_key_name(url_env, "url_env")
    validate_payload_object(payload)
    timeout = validate_timeout_seconds(timeout_seconds)
    auth_key = validate_env_key_name(authorization_env, "authorization_env") if authorization_env else None

    url = (env.get(url_key) or "").strip()
    if not url:
        raise typer.BadParameter(f"notify.webhook destination is missing: set the {url_key} environment variable")
    validate_destination_url(url)
    authorization = (env.get(auth_key) or "").strip() if auth_key else ""
    if auth_key and not authorization:
        raise typer.BadParameter(f"notify.webhook authorization is missing: set the {auth_key} environment variable")

    try:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter("notify.webhook payload must be JSON-serializable") from exc

    # Reject before sending instead of silently redacting the payload.
    blocked = _payload_secret_categories(env, url, authorization, body)
    if blocked:
        raise typer.BadParameter(
            "notify.webhook payload rejected before sending: it contains a "
            + " and a ".join(blocked)
            + "; remove it from the payload instead of sending secrets to the destination"
        )

    headers = {"Content-Type": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _open_webhook_response(request, timeout) as response:
            # The response body is deliberately never read: it may echo the
            # request and must not reach messages or saved logs.
            status = int(response.status)
    except urllib.error.HTTPError as exc:
        if exc.fp is not None:  # release the error body without reading it
            exc.close()
        # The status code is safe to report; the body is not read.
        raise WebhookError("http_error", exc.code) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as exc:
        # ``from None``: transport exceptions can embed the destination URL.
        raise WebhookError(_transport_category(exc)) from None
    # Only 2xx counts as success, independent of transport-specific error
    # behaviour (informational, redirect and server answers all fail here).
    if not 200 <= status <= 299:
        raise WebhookError("http_error", status)
    return status
