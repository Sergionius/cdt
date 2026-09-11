import email.utils
import errno
import json
import random
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import jwt
import typer

from .. import config
from ..runner import _run
from ..ui import _tracker_set

ASC_API_BASE = "https://api.appstoreconnect.apple.com"

# Token-aware ASC client tuning. Kept as module-private constants: no extra public settings.
ASC_JWT_TTL_SEC = 20 * 60
ASC_JWT_REFRESH_MARGIN_SEC = 60
ASC_REQUEST_TIMEOUT_SEC = 60
ASC_RETRY_MAX_ATTEMPTS = 4
ASC_RETRY_BASE_DELAY_SEC = 1.0
ASC_RETRY_MAX_DELAY_SEC = 15.0
ASC_RETRY_JITTER_SEC = 0.5

# OSError errnos treated as transient network failures (reset, abort, unreachable, ...).
_ASC_TRANSIENT_ERRNOS = frozenset(
    {
        errno.ECONNABORTED,
        errno.ECONNREFUSED,
        errno.ECONNRESET,
        errno.EHOSTUNREACH,
        errno.ENETDOWN,
        errno.ENETUNREACH,
        errno.EPIPE,
        errno.ETIMEDOUT,
    }
)


def _ensure_itmstransporter_available() -> None:
    try:
        check = subprocess.run(["xcrun", "iTMSTransporter", "-help"], capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise typer.BadParameter(
            "xcrun is not available. Install Xcode Command Line Tools and verify with: xcrun --version"
        ) from exc

    if check.returncode != 0:
        raise typer.BadParameter(
            "iTMSTransporter is not available. Install Xcode + Command Line Tools, "
            "then verify with: xcrun iTMSTransporter -help"
        )


def _asc_token(env: dict[str, str]) -> str:
    key_id = env.get("ASC_KEY_ID", "").strip()
    issuer_id = env.get("ASC_ISSUER_ID", "").strip()
    key_path_raw = env.get("ASC_PRIVATE_KEY_PATH", "").strip()

    if not key_id or not issuer_id or not key_path_raw:
        raise typer.BadParameter(
            "Missing ASC credentials. Required in .env: ASC_KEY_ID, ASC_ISSUER_ID, ASC_PRIVATE_KEY_PATH"
        )

    key_path = Path(key_path_raw).expanduser()
    if not key_path.is_absolute():
        key_path = Path.cwd() / key_path
    if not key_path.exists():
        raise typer.BadParameter(f"ASC private key not found: {key_path}")

    private_key = key_path.read_text(encoding="utf-8")
    now = int(time.time())
    return jwt.encode(
        {
            "iss": issuer_id,
            "aud": "appstoreconnect-v1",
            "exp": now + 20 * 60,
        },
        private_key,
        algorithm="ES256",
        headers={"kid": key_id, "typ": "JWT"},
    )


class _AscClient:
    """Token-aware App Store Connect client.

    Caches the JWT, transparently refreshes it before the 20 minute expiry and
    supports a forced refresh after a 401 response. ``deadline`` (a
    ``time.time()`` timestamp) lets request retries respect the overall polling
    deadline of ``_asc_wait_build``.
    """

    def __init__(self, env: dict[str, str], deadline: float | None = None):
        self._env = env
        self.deadline = deadline
        self._token: str | None = None
        self._token_expires_at = 0.0

    def token(self) -> str:
        if self._token is None or time.time() >= self._token_expires_at - ASC_JWT_REFRESH_MARGIN_SEC:
            self.force_token_refresh()
        assert self._token is not None
        return self._token

    def force_token_refresh(self) -> None:
        self._token = _asc_token(self._env)
        self._token_expires_at = time.time() + ASC_JWT_TTL_SEC


def _asc_transient_category(exc: BaseException) -> str | None:
    """Return a short category name when *exc* is a transient network failure, otherwise None."""
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "ssl_error"
    if isinstance(exc, ConnectionError):
        return type(exc).__name__
    if isinstance(exc, urllib.error.URLError):
        reason = exc.reason
        if isinstance(reason, BaseException):
            nested = _asc_transient_category(reason)
            if nested is not None:
                return nested
        return "url_error"
    if isinstance(exc, OSError) and getattr(exc, "errno", None) in _ASC_TRANSIENT_ERRNOS:
        return "os_error"
    return None


def _asc_retry_after_seconds(value: str | None, now: float | None = None) -> float | None:
    """Parse a Retry-After header (delay-seconds or HTTP-date) into seconds, or None if unusable."""
    if not value:
        return None
    text = value.strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        retry_at = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError):
        return None
    if retry_at is None:
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    current = datetime.fromtimestamp(time.time() if now is None else now, tz=timezone.utc)
    return max(0.0, (retry_at - current).total_seconds())


def _asc_retry_delay(attempt: int, retry_after_sec: float | None = None) -> float:
    """Bounded exponential backoff (with jitter) before the *attempt*-th retry."""
    delay = min(ASC_RETRY_BASE_DELAY_SEC * (2 ** (attempt - 1)), ASC_RETRY_MAX_DELAY_SEC)
    delay += random.uniform(0.0, ASC_RETRY_JITTER_SEC)
    if retry_after_sec is not None:
        delay = max(delay, retry_after_sec)
    return min(delay, ASC_RETRY_MAX_DELAY_SEC)


def _asc_retry_wait(delay: float, deadline: float | None, now: float | None = None) -> float:
    """Clamp a retry delay so a polling retry never sleeps past the caller's overall deadline."""
    if deadline is None:
        return delay
    remaining = deadline - (time.time() if now is None else now)
    if remaining <= 0.0:
        return 0.0
    return min(delay, remaining)


def _asc_request(method: str, path: str, client: _AscClient, payload: dict | None = None) -> dict:
    url = ASC_API_BASE + path
    attempts = 0
    refreshed_after_401 = False
    while True:
        category = "unknown"
        detail = "unknown"
        retry_after_sec: float | None = None
        req = urllib.request.Request(
            url,
            method=method,
            data=json.dumps(payload).encode("utf-8") if payload is not None else None,
            headers={
                "Authorization": f"Bearer {client.token()}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=ASC_REQUEST_TIMEOUT_SEC) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 401 and not refreshed_after_401:
                refreshed_after_401 = True
                client.force_token_refresh()
                typer.echo("==> ASC request got 401: forced JWT refresh, retrying once")
                continue
            if exc.code == 429 or exc.code >= 500:
                category = f"http_{exc.code}"
                retry_after_sec = _asc_retry_after_seconds(exc.headers.get("Retry-After") if exc.headers else None)
                detail = f"HTTP {exc.code}: {body}"
            else:
                raise typer.BadParameter(f"App Store Connect API error {exc.code}: {body}") from exc
        except (urllib.error.URLError, TimeoutError, socket.timeout, ssl.SSLError, ConnectionError, OSError) as exc:
            category = _asc_transient_category(exc)
            if category is None:
                raise typer.BadParameter(f"App Store Connect request failed: {type(exc).__name__}: {exc}") from exc
            detail = str(exc) or type(exc).__name__
        attempts += 1
        if attempts >= ASC_RETRY_MAX_ATTEMPTS:
            raise typer.BadParameter(
                f"App Store Connect request failed after {ASC_RETRY_MAX_ATTEMPTS} attempts "
                f"due to transient failures (last: {category}: {detail})"
            )
        delay = _asc_retry_wait(_asc_retry_delay(attempts, retry_after_sec), client.deadline)
        typer.echo(
            f"==> ASC transient failure, attempt {attempts}/{ASC_RETRY_MAX_ATTEMPTS} "
            f"({category}); retrying in {delay:.1f}s"
        )
        time.sleep(delay)


def _asc_get_app_id(bundle_id: str, client: _AscClient) -> str:
    q = urllib.parse.quote(bundle_id)
    rsp = _asc_request("GET", f"/v1/apps?filter[bundleId]={q}&limit=1", client)
    items = rsp.get("data", [])
    if not items:
        raise typer.BadParameter(f"App not found in App Store Connect for bundle id: {bundle_id}")
    return items[0]["id"]


def _asc_wait_build(app_id: str, build_number: str, client: _AscClient, timeout_sec: int = 30) -> tuple[str, str]:
    started = time.time()
    deadline = started + timeout_sec
    client.deadline = deadline
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        now = time.time()
        elapsed = int(now - started)
        left = max(0, int(deadline - now))

        path = (
            "/v1/builds"
            f"?filter[app]={urllib.parse.quote(app_id)}"
            f"&filter[version]={urllib.parse.quote(build_number)}"
            "&limit=1"
        )
        if config.UI_MODE == "pretty":
            _tracker_set(
                "App Store Connect",
                f"Проверка статуса (попытка {attempt}, прошло {elapsed}s, осталось {left}s)",
            )
        else:
            typer.echo(f"==> ASC poll #{attempt}: querying build={build_number} (elapsed={elapsed}s, left={left}s)")

        rsp = _asc_request("GET", path, client)
        items = rsp.get("data", [])
        if not items:
            if config.UI_MODE == "pretty":
                _tracker_set("App Store Connect", "Билд ещё не появился")
            else:
                typer.echo("==> ASC poll result: build not visible yet")
        else:
            item = items[0]
            state = item.get("attributes", {}).get("processingState", "UNKNOWN")
            build_id = item["id"]
            if config.UI_MODE == "pretty":
                _tracker_set("App Store Connect", f"Статус: {state}")
            else:
                typer.echo(f"==> ASC poll result: build_id={build_id}, processingState={state}")
            if state in {"VALID", "FAILED", "INVALID"}:
                return build_id, state

        if time.time() < deadline:
            if config.UI_MODE == "pretty":
                _tracker_set("App Store Connect", "Ждём следующий ответ (30s)")
            else:
                typer.echo("==> waiting 30s before next ASC poll")
            time.sleep(30)

    raise typer.BadParameter("Timeout waiting for build processing in TestFlight")


def _asc_set_changelog(build_id: str, changelog: str, client: _AscClient) -> None:
    query = f"/v1/betaBuildLocalizations?filter[build]={urllib.parse.quote(build_id)}&filter[locale]=en-US&limit=1"
    existing = _asc_request("GET", query, client).get("data", [])

    if existing:
        loc_id = existing[0]["id"]
        _asc_request(
            "PATCH",
            f"/v1/betaBuildLocalizations/{loc_id}",
            client,
            {
                "data": {
                    "type": "betaBuildLocalizations",
                    "id": loc_id,
                    "attributes": {"whatsNew": changelog},
                }
            },
        )
        return

    _asc_request(
        "POST",
        "/v1/betaBuildLocalizations",
        client,
        {
            "data": {
                "type": "betaBuildLocalizations",
                "attributes": {"locale": "en-US", "whatsNew": changelog},
                "relationships": {
                    "build": {"data": {"type": "builds", "id": build_id}}
                },
            }
        },
    )


def _build_testflight_transporter_command(ipa_path: Path, env: dict[str, str]) -> list[str]:
    _ensure_itmstransporter_available()

    api_key = env.get("ASC_KEY_ID", "").strip()
    api_issuer = env.get("ASC_ISSUER_ID", "").strip()
    key_path_raw = env.get("ASC_PRIVATE_KEY_PATH", "").strip()
    key_path = Path(key_path_raw).expanduser()
    if not key_path.is_absolute():
        key_path = Path.cwd() / key_path

    return [
        "xcrun",
        "iTMSTransporter",
        "-m",
        "upload",
        "-assetFile",
        str(ipa_path),
        "-apiKey",
        api_key,
        "-apiIssuer",
        api_issuer,
        "-apiKeyPath",
        str(key_path.parent),
    ]


def _complete_testflight_after_upload(env: dict[str, str], changelog: str, new_version: str) -> int:
    bundle_id = env.get("IOS_BUNDLE_ID", "").strip()
    if not bundle_id:
        raise typer.BadParameter("Missing IOS_BUNDLE_ID in project .env")

    build_number = new_version.rsplit("+", 1)[1]

    wait_timeout_raw = env.get("ASC_WAIT_TIMEOUT_SEC", "30").strip()
    try:
        wait_timeout_sec = int(wait_timeout_raw)
    except ValueError as exc:
        raise typer.BadParameter("ASC_WAIT_TIMEOUT_SEC must be an integer") from exc

    client = _AscClient(env)
    app_id = _asc_get_app_id(bundle_id, client)
    if config.UI_MODE == "pretty":
        _tracker_set("App Store Connect", f"Ожидаем обработку build {build_number}")
    else:
        typer.echo(f"==> Waiting for TestFlight processing (build {build_number}, timeout={wait_timeout_sec}s)")
    build_id, state = _asc_wait_build(app_id, build_number, client, timeout_sec=wait_timeout_sec)
    if state != "VALID":
        raise typer.BadParameter(f"Build processing ended with state: {state}")

    if config.UI_MODE == "pretty":
        _tracker_set("TestFlight changelog", "running")
    else:
        typer.echo("==> Setting TestFlight changelog")
    _asc_set_changelog(build_id, changelog, client)
    return 0


def _upload_testflight_ipa(ipa_path: Path, env: dict[str, str]) -> int:
    """Run only the iTMSTransporter upload for a ready IPA artifact.

    Contains no post-upload ASC processing so a resume of a completed upload
    can continue with ``_complete_testflight_after_upload`` without re-uploading.
    """
    transporter_cmd = _build_testflight_transporter_command(ipa_path, env)
    return _run(transporter_cmd, cwd=Path.cwd())


def _upload_testflight(ipa_path: Path, env: dict[str, str], changelog: str, new_version: str) -> int:
    """Backwards-compatible orchestrator: full upload cycle in a single step."""
    upload_status = _upload_testflight_ipa(ipa_path, env)
    if upload_status != 0:
        return upload_status
    return _complete_testflight_after_upload(env, changelog, new_version)
