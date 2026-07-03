from cdt.artifacts import ArtifactKind
from cdt.platforms.ios_flutter import _build_ios_prod_ipa_command, _build_ios_test_ipa_command, _ios_ipa_artifact


def test_ios_build_commands():
    assert _build_ios_test_ipa_command() == [
        "flutter",
        "build",
        "ipa",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--no-pub",
    ]
    assert _build_ios_prod_ipa_command() == [
        "flutter",
        "build",
        "ipa",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--dart-define=ENV=prod",
        "--no-pub",
    ]


def test_ios_build_command_accepts_custom_flutter_options():
    assert _build_ios_test_ipa_command(
        dart_defines=["ENV=qa", "API=mock"],
        flavor="qa",
        target="lib/main_qa.dart",
        obfuscate=False,
        split_debug_info=None,
        no_pub=False,
        extra_args=["--export-method=ad-hoc"],
    ) == [
        "flutter",
        "build",
        "ipa",
        "--flavor",
        "qa",
        "--target",
        "lib/main_qa.dart",
        "--dart-define=ENV=qa",
        "--dart-define=API=mock",
        "--export-method=ad-hoc",
    ]


def test_ios_prod_build_command_merges_default_dart_defines():
    assert _build_ios_prod_ipa_command(dart_defines={"API": "prod"}) == [
        "flutter",
        "build",
        "ipa",
        "--obfuscate",
        "--split-debug-info=obfsymbols",
        "--dart-define=ENV=prod",
        "--dart-define=API=prod",
        "--no-pub",
    ]


def test_ios_ipa_artifact_wraps_found_ipa(tmp_path):
    ipa = tmp_path / "build" / "ios" / "ipa" / "Runner.ipa"
    ipa.parent.mkdir(parents=True)
    ipa.write_text("ipa", encoding="utf-8")

    artifact = _ios_ipa_artifact(tmp_path)

    assert artifact.kind == ArtifactKind.IPA
    assert artifact.path == ipa
    assert artifact.label == "iOS IPA"
