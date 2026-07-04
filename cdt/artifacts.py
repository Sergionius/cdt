from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class ArtifactKind(str, Enum):
    APK = "apk"
    AAB = "aab"
    IPA = "ipa"
    WEB = "web"


@dataclass(frozen=True)
class BuildArtifact:
    kind: ArtifactKind
    path: Path
    label: str
    step: str | None = None

    def to_json(self, name: str) -> dict[str, str]:
        payload = {
            "name": name,
            "path": str(self.path),
            "kind": self.kind.value,
        }
        if self.step:
            payload["step"] = self.step
        return payload
