import json
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import jwt
import typer

from .. import config
from ..runner import _run
from ..ui import _tracker_set

ASC_API_BASE = "https://api.appstoreconnect.apple.com"


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


def _asc_request(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    url = ASC_API_BASE + path
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        url,
        method=method,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise typer.BadParameter(f"App Store Connect API error {exc.code}: {body}") from exc


def _asc_get_app_id(bundle_id: str, token: str) -> str:
    q = urllib.parse.quote(bundle_id)
    rsp = _asc_request("GET", f"/v1/apps?filter[bundleId]={q}&limit=1", token)
    items = rsp.get("data", [])
    if not items:
        raise typer.BadParameter(f"App not found in App Store Connect for bundle id: {bundle_id}")
    return items[0]["id"]


def _asc_wait_build(app_id: str, build_number: str, token: str, timeout_sec: int = 30) -> tuple[str, str]:
    started = time.time()
    deadline = started + timeout_sec
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

        rsp = _asc_request("GET", path, token)
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


def _asc_set_changelog(build_id: str, changelog: str, token: str) -> None:
    query = f"/v1/betaBuildLocalizations?filter[build]={urllib.parse.quote(build_id)}&filter[locale]=en-US&limit=1"
    existing = _asc_request("GET", query, token).get("data", [])

    if existing:
        loc_id = existing[0]["id"]
        _asc_request(
            "PATCH",
            f"/v1/betaBuildLocalizations/{loc_id}",
            token,
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
        token,
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
    token = _asc_token(env)
    bundle_id = env.get("IOS_BUNDLE_ID", "").strip()
    if not bundle_id:
        raise typer.BadParameter("Missing IOS_BUNDLE_ID in project .env")

    build_number = new_version.rsplit("+", 1)[1]

    wait_timeout_raw = env.get("ASC_WAIT_TIMEOUT_SEC", "30").strip()
    try:
        wait_timeout_sec = int(wait_timeout_raw)
    except ValueError as exc:
        raise typer.BadParameter("ASC_WAIT_TIMEOUT_SEC must be an integer") from exc

    app_id = _asc_get_app_id(bundle_id, token)
    if config.UI_MODE == "pretty":
        _tracker_set("App Store Connect", f"Ожидаем обработку build {build_number}")
    else:
        typer.echo(f"==> Waiting for TestFlight processing (build {build_number}, timeout={wait_timeout_sec}s)")
    build_id, state = _asc_wait_build(app_id, build_number, token, timeout_sec=wait_timeout_sec)
    if state != "VALID":
        raise typer.BadParameter(f"Build processing ended with state: {state}")

    if config.UI_MODE == "pretty":
        _tracker_set("TestFlight changelog", "running")
    else:
        typer.echo("==> Setting TestFlight changelog")
    _asc_set_changelog(build_id, changelog, token)
    return 0


def _upload_testflight(ipa_path: Path, env: dict[str, str], changelog: str, new_version: str) -> int:
    transporter_cmd = _build_testflight_transporter_command(ipa_path, env)
    upload_status = _run(transporter_cmd, cwd=Path.cwd())
    if upload_status != 0:
        return upload_status
    return _complete_testflight_after_upload(env, changelog, new_version)
