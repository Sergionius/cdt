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
