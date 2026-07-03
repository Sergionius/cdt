import json
import urllib.parse
import urllib.request

import typer


def _build_notify_text(env: dict[str, str], new_version: str, ids: list[str] | None = None) -> str:
    lines: list[str] = []
    custom = env.get("NOTIFY_TEXT", "").strip()
    if custom:
        lines.append(custom)

    lines.append(f"💡 Version: {new_version}")

    clean_ids = [x.strip() for x in (ids or []) if x and x.strip()]
    if clean_ids:
        lines.append("")
        lines.append("Задачи:")
        lines.extend([f"https://tracker.yandex.ru/{item}" for item in clean_ids])

    return "\n".join(lines)


def _split_version(version: str) -> tuple[str, str]:
    if "+" not in version:
        return version, ""
    major, build = version.rsplit("+", 1)
    return major, build


def _notify_prod_user_agent_pachca(env: dict[str, str], version: str) -> None:
    provider = env.get("NOTIFY_PROVIDER", "").strip().lower()
    if provider != "pachca":
        return

    webhook = env.get("PACHCA_USER_AGENT_WEBHOOK_URL", "").strip()
    if not webhook:
        raise typer.BadParameter("Missing PACHCA_USER_AGENT_WEBHOOK_URL for prod user-agent notify")

    app_name = env.get("UA_APP_NAME", "").strip()
    if not app_name:
        raise typer.BadParameter("Missing UA_APP_NAME in .env")

    major, build = _split_version(version)
    title = env.get("UA_TITLE", f"{app_name} user-agent").strip()
    ios_device = env.get("UA_IOS_DEVICE", "ios 17.0 iPhone ios-device-id").strip()
    android_device = env.get(
        "UA_ANDROID_DEVICE",
        "android 29 Android android-device-id",
    ).strip()
    user_agent_info = (
        f"{title} for version {major}:\n\n"
        f"iOS: {app_name} {major} ({build}) {ios_device}\n"
        f"android: {app_name} {major} ({build}) {android_device}"
    )

    data = json.dumps({"message": user_agent_info}).encode("utf-8")
    req = urllib.request.Request(
        webhook,
        method="POST",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status >= 400:
            raise typer.BadParameter(f"Pachca user-agent notify failed with status {resp.status}")


def _notify_success(env: dict[str, str], new_version: str, ids: list[str] | None = None) -> None:
    provider = env.get("NOTIFY_PROVIDER", "").strip().lower()
    if not provider:
        return

    text = _build_notify_text(env, new_version, ids)

    if provider == "telegram":
        token = env.get("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = env.get("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            raise typer.BadParameter("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID for telegram notify")

        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            method="POST",
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status >= 400:
                raise typer.BadParameter(f"Telegram notify failed with status {resp.status}")
        return

    if provider == "pachca":
        webhook = env.get("PACHCA_WEBHOOK_URL", "").strip()
        if not webhook:
            raise typer.BadParameter("Missing PACHCA_WEBHOOK_URL for pachca notify")

        data = json.dumps({"message": text}).encode("utf-8")
        req = urllib.request.Request(
            webhook,
            method="POST",
            data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status >= 400:
                raise typer.BadParameter(f"Pachca notify failed with status {resp.status}")
        return

    raise typer.BadParameter("NOTIFY_PROVIDER must be telegram or pachca")
