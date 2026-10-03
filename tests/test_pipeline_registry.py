import pytest
import typer

from cdt.pipeline.builtins import register_builtin_steps
from cdt.pipeline.registry import (
    ResultProduction,
    ResultRequirement,
    StepMetadata,
    _clear_steps_for_tests,
    get_step_factory,
    get_step_metadata,
    list_step_metadata,
    list_steps,
    register_step,
)
from cdt.sdk import step as sdk_step


class DummyStep:
    name = "dummy"

    def run(self, ctx):
        return None


def setup_function():
    _clear_steps_for_tests()


def teardown_function():
    _clear_steps_for_tests()


def test_register_and_get_step_factory():
    register_step("demo.step", DummyStep)

    assert get_step_factory("demo.step") is DummyStep
    assert list_steps() == ["demo.step"]


def test_register_step_metadata():
    metadata = StepMetadata(
        name="demo.step",
        description="Demo step",
        category="demo",
        risk="safe",
        external_tools=("demo",),
    )
    register_step("demo.step", DummyStep, metadata=metadata)

    registered = get_step_metadata("demo.step")
    assert registered.description == "Demo step"
    assert registered.category == "demo"
    assert registered.risk == "safe"
    assert registered.external_tools == ("demo",)
    assert list_step_metadata() == [registered]


def test_builtin_metadata_registration():
    register_builtin_steps()

    flutter = get_step_metadata("flutter.pub_get")
    appstore = get_step_metadata("appstore.upload_testflight")
    upload_only = get_step_metadata("appstore.upload_testflight_ipa")
    completion = get_step_metadata("appstore.complete_testflight")
    submit_review = get_step_metadata("appstore.submit_review")
    ios_ipa = get_step_metadata("ios.flutter_build_ipa")
    android_aab = get_step_metadata("android.build_aab")
    android_apk = get_step_metadata("android.build_apk")
    firebase = get_step_metadata("firebase.upload_app_distribution")
    prod_user_agent = get_step_metadata("notify.prod_user_agent")

    assert flutter.category == "flutter"
    assert flutter.risk == "safe"
    assert flutter.external_tools == ("flutter",)
    assert appstore.category == "appstore"
    assert appstore.risk == "upload"
    assert appstore.requires == (ResultRequirement(("ios_ipa",), name_options=("artifact",)),)
    assert appstore.produces == (ResultProduction("upload_result"),)
    assert upload_only.requires == (ResultRequirement(("ios_ipa",), name_options=("artifact",)),)
    assert upload_only.produces == (ResultProduction("upload_result"),)
    assert upload_only.external_tools == ("xcrun",)
    assert upload_only.requires_env == ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY_PATH")
    assert completion.requires == ()
    assert completion.produces == ()
    assert completion.external_tools == ()
    assert completion.requires_env == ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY_PATH", "IOS_BUNDLE_ID")
    # The review submission step needs no artifact and no build tooling: the app
    # comes from IOS_BUNDLE_ID and the build from the recorded completion.
    assert submit_review.category == "appstore"
    assert submit_review.risk == "upload"
    assert submit_review.requires == ()
    assert submit_review.produces == (ResultProduction("review_submission"),)
    assert submit_review.external_tools == ()
    assert submit_review.requires_env == ("ASC_KEY_ID", "ASC_ISSUER_ID", "ASC_PRIVATE_KEY_PATH", "IOS_BUNDLE_ID")
    assert ios_ipa.produces == (ResultProduction("ios_ipa", name_options=("artifact",)),)
    assert android_aab.produces == (ResultProduction("android_aab", name_options=("artifact",)),)
    assert android_apk.produces == (ResultProduction("android_apk", name_options=("artifact",)),)
    assert firebase.requires == (
        ResultRequirement(("android_aab", "android_apk"), mode="any", name_options=("artifact",)),
    )
    assert prod_user_agent.category == "notify"
    assert prod_user_agent.risk == "upload"
    assert prod_user_agent.requires_env == ("PACHCA_USER_AGENT_WEBHOOK_URL", "UA_APP_NAME")

    google_play = get_step_metadata("google_play.upload_aab")
    assert google_play.category == "google_play"
    assert google_play.risk == "upload"
    assert google_play.requires == (ResultRequirement(("android_aab",), name_options=("artifact",)),)
    assert google_play.produces == (ResultProduction("upload_result"),)
    # ADC is optional in preflight; it is never declared as a required variable.
    assert google_play.requires_env == ()
    assert google_play.external_tools == ()


def test_step_metadata_to_dict_is_structured():
    metadata = StepMetadata(
        name="demo.step",
        description="Demo step",
        category="demo",
        risk="safe",
        requires=(ResultRequirement(("ios_ipa",), name_options=("artifact",)),),
        produces=(ResultProduction("upload_result"),),
        external_tools=("demo",),
        requires_env=("DEMO_TOKEN",),
        retry_safe=True,
    )

    assert metadata.to_dict() == {
        "name": "demo.step",
        "description": "Demo step",
        "category": "demo",
        "risk": "safe",
        "requires": [
            {"result_types": ["ios_ipa"], "mode": "all", "name_options": ["artifact"]},
        ],
        "produces": [
            {"result_type": "upload_result", "name_options": []},
        ],
        "external_tools": ["demo"],
        "requires_env": ["DEMO_TOKEN"],
        "plugin": False,
        "retry_safe": True,
        "timeout_option": None,
    }


def test_step_metadata_timeout_option_defaults_to_none_and_normalizes():
    register_step("demo.plain", DummyStep)
    register_step(
        "demo.timed",
        DummyStep,
        metadata=StepMetadata(name="demo.timed", timeout_option="timeout"),
    )
    register_step(
        "demo.spaced",
        DummyStep,
        metadata=StepMetadata(name="demo.spaced", timeout_option=" timeout "),
    )

    assert get_step_metadata("demo.plain").timeout_option is None
    assert get_step_metadata("demo.timed").timeout_option == "timeout"
    # Registration normalizes into a fresh metadata object and keeps the capability.
    assert get_step_metadata("demo.spaced").timeout_option == "timeout"
    assert get_step_metadata("demo.timed").to_dict()["timeout_option"] == "timeout"

    with pytest.raises(ValueError, match="timeout_option must be a non-empty string or None"):
        StepMetadata(name="demo.bad", timeout_option="")
    with pytest.raises(ValueError, match="timeout_option must be a non-empty string or None"):
        StepMetadata(name="demo.bad", timeout_option=5)


def test_builtin_timeout_capability_belongs_to_hook_only():
    from cdt.pipeline.builtins import _BUILTIN_METADATA

    timed = {name: metadata.timeout_option for name, metadata in _BUILTIN_METADATA.items() if metadata.timeout_option}
    assert timed == {"hook.python_script": "timeout"}


def test_step_metadata_defaults_to_not_retry_safe_and_normalizes_to_bool():
    register_step("demo.plain", DummyStep)
    register_step("demo.retryable", DummyStep, metadata=StepMetadata(name="demo.retryable", retry_safe=True))
    register_step("demo.coerced", DummyStep, metadata=StepMetadata(name="demo.coerced", retry_safe="yes"))

    assert get_step_metadata("demo.plain").retry_safe is False
    assert get_step_metadata("demo.retryable").retry_safe is True
    assert get_step_metadata("demo.coerced").retry_safe is True
    # Registration normalizes into a fresh metadata object and keeps the capability.
    normalized = get_step_metadata("demo.retryable")
    assert normalized.to_dict()["retry_safe"] is True


def test_retryable_step_error_is_exported_from_sdk():
    from cdt.pipeline.policy import RetryableStepError as PolicyError
    from cdt.sdk import RetryableStepError as SdkError

    assert SdkError is PolicyError


def test_result_requirement_rejects_invalid_mode():
    with pytest.raises(ValueError, match="mode must be 'all' or 'any'"):
        ResultRequirement(("ios_ipa",), mode="some")


def test_result_requirement_rejects_empty_result_types():
    with pytest.raises(ValueError, match="result_types cannot be empty"):
        ResultRequirement(())


def test_result_production_rejects_empty_result_type():
    with pytest.raises(ValueError, match="result_type cannot be empty"):
        ResultProduction("")


def test_result_metadata_normalizes_string_lists_to_tuples():
    requirement = ResultRequirement(["ios_ipa"], name_options=["artifact"])
    production = ResultProduction("upload_result", name_options=["artifact"])

    assert requirement.result_types == ("ios_ipa",)
    assert requirement.name_options == ("artifact",)
    assert production.name_options == ("artifact",)


def test_duplicate_step_registration_errors():
    register_step("demo.step", DummyStep)

    with pytest.raises(typer.BadParameter, match="already registered"):
        register_step("demo.step", DummyStep)


def test_unknown_step_error_lists_available_steps():
    register_step("demo.step", DummyStep)

    with pytest.raises(typer.BadParameter, match="Unknown pipeline step: missing.step"):
        get_step_factory("missing.step")


def test_sdk_step_accepts_keyword_metadata():
    @sdk_step(
        "demo.fetch",
        description="Fetch demo data",
        category="demo",
        risk="safe",
        produces=[ResultProduction("json")],
    )
    def fetch(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("demo.fetch")
    assert metadata.description == "Fetch demo data"
    assert metadata.category == "demo"
    assert metadata.risk == "safe"
    assert metadata.produces == (ResultProduction("json"),)
    assert metadata.plugin is True


def test_sdk_step_accepts_requires_and_produces():
    @sdk_step(
        "demo.upload",
        requires=[ResultRequirement(("ios_ipa",), name_options=("artifact",))],
        produces=[ResultProduction("upload_result")],
    )
    def upload(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("demo.upload")
    assert metadata.requires == (ResultRequirement(("ios_ipa",), name_options=("artifact",)),)
    assert metadata.produces == (ResultProduction("upload_result"),)
    assert metadata.plugin is True


def test_sdk_step_rejects_metadata_with_requires_or_produces():
    given = StepMetadata(name="demo.fetch")

    with pytest.raises(TypeError, match="Cannot pass both 'metadata' and 'requires'/'produces'"):

        @sdk_step("demo.fetch", metadata=given, requires=[ResultRequirement(("ios_ipa",))])
        def fetch(ctx, output: str) -> None:
            pass


def test_sdk_step_accepts_metadata_object():
    given = StepMetadata(
        name="demo.fetch",
        description="Fetch via object",
        category="demo",
        risk="upload",
        external_tools=("curl",),
    )

    @sdk_step("demo.fetch", metadata=given)
    def fetch(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("demo.fetch")
    assert metadata.description == "Fetch via object"
    assert metadata.risk == "upload"
    assert metadata.external_tools == ("curl",)
    assert metadata.plugin is True


def test_sdk_step_defaults_category_from_name_and_custom_risk():
    @sdk_step("offline.sync")
    def sync(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("offline.sync")
    assert metadata.category == "offline"
    assert metadata.risk == "custom"
    assert metadata.plugin is True
    # The capability is opt-in: the SDK default keeps retries disabled.
    assert metadata.retry_safe is False


def test_sdk_step_accepts_retry_safe_keyword():
    @sdk_step("demo.transient", retry_safe=True)
    def fetch(ctx, output: str) -> None:
        pass

    assert get_step_metadata("demo.transient").retry_safe is True


def test_sdk_step_metadata_object_keeps_retry_safe():
    given = StepMetadata(name="demo.transient", retry_safe=True)

    @sdk_step("demo.transient", metadata=given)
    def fetch(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("demo.transient")
    assert metadata.plugin is True
    assert metadata.retry_safe is True


def test_sdk_step_accepts_timeout_option_keyword():
    @sdk_step("demo.timed", timeout_option="timeout")
    def timed(ctx, timeout=None) -> None:
        pass

    assert get_step_metadata("demo.timed").timeout_option == "timeout"


def test_sdk_step_metadata_object_keeps_timeout_option():
    given = StepMetadata(name="demo.timed", timeout_option="timeout")

    @sdk_step("demo.timed", metadata=given)
    def timed(ctx, timeout=None) -> None:
        pass

    metadata = get_step_metadata("demo.timed")
    assert metadata.plugin is True
    assert metadata.timeout_option == "timeout"


def test_sdk_step_defaults_custom_category_for_flat_names():
    @sdk_step("sync")
    def sync(ctx, output: str) -> None:
        pass

    metadata = get_step_metadata("sync")
    assert metadata.category == "custom"
    assert metadata.risk == "custom"
    assert metadata.plugin is True
