from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_CREDENTIAL_KEY_RE = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY|API_KEY|AUTH|CREDENTIAL",
    re.IGNORECASE,
)
_AUTHORIZATION_RE = re.compile(r"(?im)(\bauthorization\s*:\s*)(?:bearer\s+)?[^\s]+")
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+")
_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|secret|password|passwd|api[_-]?key|auth|credential)"
    r"(\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;]+)"
)
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}\b")


@dataclass(frozen=True)
class SecretRedactor:
    """Redact known credential values and conservative credential-shaped text."""

    secrets: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> SecretRedactor:
        extra_keys = {
            key.strip()
            for key in env.get("CDT_REDACT_KEYS", "").split(",")
            if key.strip()
        }
        values: set[str] = set()
        for key, value in env.items():
            if not isinstance(value, str) or not (_CREDENTIAL_KEY_RE.search(key) is not None or key in extra_keys):
                continue
            candidates = [value, *value.splitlines()]
            values.update(candidate for candidate in candidates if len(candidate) >= 4)
        return cls(tuple(sorted(values, key=lambda value: (-len(value), value))))

    def redact(self, text: str) -> str:
        result = text
        for secret in self.secrets:
            result = result.replace(secret, "***")
        result = _AUTHORIZATION_RE.sub(r"\1***", result)
        result = _BEARER_RE.sub("Bearer ***", result)
        result = _ASSIGNMENT_RE.sub(r"\1\2***", result)
        return _JWT_RE.sub("***", result)

    def redact_data(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, dict):
            return {key: self.redact_data(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self.redact_data(item) for item in value]
        if isinstance(value, tuple):
            return tuple(self.redact_data(item) for item in value)
        return value


class StreamingRedactor:
    """Redact complete streamed lines while retaining incomplete line fragments."""

    def __init__(self, redactor: SecretRedactor, *, max_pending: int = 1024 * 1024):
        self._redactor = redactor
        self._max_pending = max_pending
        self._pending = ""
        self._discarding_oversized_line = False

    def feed(self, text: str, *, final: bool = False) -> str:
        pending = self._pending + text
        self._pending = ""
        output: list[str] = []

        if self._discarding_oversized_line:
            newline = pending.find("\n")
            if newline < 0:
                if final:
                    self._discarding_oversized_line = False
                return ""
            self._discarding_oversized_line = False
            pending = pending[newline + 1 :]

        last_newline = pending.rfind("\n")
        if last_newline >= 0:
            output.append(self._redactor.redact(pending[: last_newline + 1]))
            pending = pending[last_newline + 1 :]

        if final:
            output.append(self._redactor.redact(pending))
        elif len(pending) > self._max_pending:
            output.append("*** [CDT redacted oversized output line]\n")
            self._discarding_oversized_line = True
        else:
            self._pending = pending

        return "".join(output)
