import json
import urllib.request

import typer


def _build_tracker_release_notes(ids: list[str]) -> str:
    return "\n".join([f"https://tracker.yandex.ru/{item}" for item in ids])


def _build_tracker_comment_text(env: dict[str, str], new_version: str) -> str:
    lines: list[str] = []
    custom = env.get("NOTIFY_TEXT", "").strip()
    if custom:
        lines.append(custom)

    lines.append(f"💡 Версия: {new_version}")
    return "\n".join(lines)


def _tracker_comment(env: dict[str, str], issue_id: str, new_version: str) -> None:
    token = env.get("TRACKER_OAUTH_TOKEN", "").strip()
    org_id = env.get("TRACKER_ORG_ID", "").strip()
    base_url = env.get("TRACKER_BASE_URL", "https://api.tracker.yandex.net").strip().rstrip("/")

    if not token or not org_id:
        raise typer.BadParameter("Missing TRACKER_OAUTH_TOKEN or TRACKER_ORG_ID for tracker comment")

    url = f"{base_url}/v2/issues/{issue_id}/comments"
    data = json.dumps({"text": _build_tracker_comment_text(env, new_version)}).encode("utf-8")
    req = urllib.request.Request(
        url,
        method="POST",
        data=data,
        headers={
            "Authorization": f"OAuth {token}",
            "X-Cloud-Org-Id": org_id,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        if resp.status >= 400:
            raise typer.BadParameter(f"Tracker comment failed for {issue_id}: status {resp.status}")
