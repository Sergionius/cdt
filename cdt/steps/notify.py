from typing import Any

import typer

from ..pipeline import PipelineContext
from ..services.notify import _notify_prod_user_agent_pachca, _notify_success
from ..services.webhook import (
    DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
    WebhookError,
    send_webhook,
    validate_env_key_name,
    validate_fail_on_error,
    validate_payload_object,
    validate_timeout_seconds,
)
from ..sounds import _play_success_sound


class NotifySuccessStep:
    name = "notify.success"

    def __init__(self, message: str | None = None, include_ids: bool = False):
        self.message = message
        self.include_ids = include_ids

    def run(self, ctx: PipelineContext) -> None:
        if not ctx.new_version:
            raise typer.BadParameter("Missing pipeline value: new_version")

        if self.message:
            typer.echo(f"✅ {self.message}")
        elif ctx.pipeline_name:
            typer.echo(f"✅ Pipeline '{ctx.pipeline_name}' completed")
        else:
            typer.echo("✅ Pipeline completed")
        issue_ids = ctx.ids if self.include_ids and ctx.ids else None
        try:
            _notify_success(ctx.env, ctx.new_version, issue_ids)
            if ctx.env.get("NOTIFY_PROVIDER", "").strip():
                typer.echo("==> Success notification sent")
        except Exception as exc:
            typer.echo(f"⚠️ Notification failed: {exc}", err=True)


class NotifyWebhookStep:
    """Deliver one explicit JSON payload to an HTTPS webhook endpoint.

    Strict by default: exactly one POST, verified TLS, no redirects, no
    automatic retries, and only 2xx responses count as successful. The
    destination URL, the authorization value, the payload and the response
    body never appear in messages or saved logs; failures are reported as a
    safe category plus the HTTP status when available. ``fail_on_error: false``
    downgrades delivery failures (network, timeout, non-2xx) to warnings;
    configuration errors always fail the step.
    """

    name = "notify.webhook"

    def __init__(
        self,
        url_env: str,
        payload: dict[str, Any],
        authorization_env: str | None = None,
        timeout_seconds: float = DEFAULT_WEBHOOK_TIMEOUT_SECONDS,
        fail_on_error: bool = True,
    ):
        self.url_env = validate_env_key_name(url_env, "url_env")
        self.payload = validate_payload_object(payload)
        self.authorization_env = (
            validate_env_key_name(authorization_env, "authorization_env") if authorization_env is not None else None
        )
        self.timeout_seconds = validate_timeout_seconds(timeout_seconds)
        self.fail_on_error = validate_fail_on_error(fail_on_error)

    def run(self, ctx: PipelineContext) -> None:
        typer.echo("==> Sending webhook notification")
        try:
            status = send_webhook(
                ctx.env,
                url_env=self.url_env,
                payload=self.payload,
                authorization_env=self.authorization_env,
                timeout_seconds=self.timeout_seconds,
            )
        except WebhookError as exc:
            # Safe category (plus HTTP status) only: never the URL, the
            # authorization value, the payload or the raw transport exception.
            message = f"notify.webhook failed: {exc}"
            if self.fail_on_error:
                # ``from None`` keeps transport exceptions that can embed the
                # destination URL out of any saved traceback.
                raise typer.BadParameter(message) from None
            typer.echo(f"⚠️ {message}", err=True)
            return
        typer.echo(f"==> Webhook notification delivered (HTTP {status})")


class PlaySuccessSoundStep:
    name = "notify.play_success_sound"

    def run(self, ctx: PipelineContext) -> None:
        _play_success_sound(ctx.env, ctx.cwd)


class NotifyProdUserAgentPachcaStep:
    name = "notify.prod_user_agent"

    def run(self, ctx: PipelineContext) -> None:
        if not ctx.new_version:
            raise typer.BadParameter("Missing pipeline value: new_version")

        try:
            _notify_prod_user_agent_pachca(ctx.env, ctx.new_version)
            if ctx.env.get("NOTIFY_PROVIDER", "").strip().lower() == "pachca":
                typer.echo("==> Pachca prod user-agent notification sent")
        except Exception as exc:
            typer.echo(f"⚠️ Pachca prod user-agent notification failed: {exc}", err=True)
