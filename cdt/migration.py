from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import typer

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover
    yaml = None

LEGACY_PIPELINE_NAMES = ["prod", "test", "deploy", "ios-prod", "ios-test", "firebase-deploy"]


def legacy_pipeline_defs() -> dict[str, dict[str, Any]]:
    return {
        "prod": {
            "steps": [
                "flutter.increment_build_number",
                "flutter.pub_get",
                {"parallel": {"steps": [
                    {"ios.flutter_build_ipa": {"profile": "prod", "flavor": "prod", "artifact": "ios_ipa"}},
                    {"android.build_aab": {"profile": "prod", "flavor": "prod", "artifact": "android_aab"}},
                ]}},
                {"parallel": {"steps": [
                    {"appstore.upload_testflight": {"artifact": "ios_ipa", "changelog": "prod build"}},
                    {"artifact.copy_to_downloads": {"artifact": "android_aab"}},
                ]}},
                {"android.build_apk": {
                    "profile": "prod",
                    "flavor": "prod",
                    "artifact": "android_apk",
                    "dart_defines": {"STORE": "ru"},
                }},
                {"artifact.copy_to_downloads": {"artifact": "android_apk"}},
                "notify.success",
            ]
        },
        "test": {
            "steps": [
                "flutter.increment_build_number",
                "flutter.pub_get",
                {"android.build_aab": {"profile": "test", "artifact": "android_aab"}},
            ]
        },
        "deploy": {
            "steps": [
                "git.prepare_clean_main",
                {"web.build": {"env": "prod"}},
                {
                    "web.copy": {
                        "repository": "${WEB_REPOSITORY}",
                        "destination": "${WEB_BUILD_PLACE}",
                        "inner": "${WEB_INNER}",
                    }
                },
                {"web.cache_bust": {"destination": "${WEB_BUILD_PLACE}", "inner": "${WEB_INNER}"}},
                {"git.commit_push": {"repository": "${WEB_REPOSITORY}", "message": "${flutter.version}"}},
                "notify.success",
            ]
        },
        "ios-prod": {
            "steps": [
                "flutter.increment_build_number",
                "flutter.pub_get",
                {"ios.flutter_build_ipa": {"profile": "prod", "artifact": "ios_ipa"}},
                {"appstore.upload_testflight": {"artifact": "ios_ipa", "changelog": "prod build"}},
                "notify.success",
            ]
        },
        "ios-test": {
            "steps": [
                "flutter.increment_build_number",
                "flutter.pub_get",
                {"ios.flutter_build_ipa": {"profile": "test", "artifact": "ios_ipa"}},
                {"appstore.upload_testflight": {"artifact": "ios_ipa", "changelog": "dev build"}},
                "notify.success",
            ]
        },
        "firebase-deploy": {"steps": ["web.build", "firebase.deploy", "notify.success"]},
    }


def migrate_legacy(cwd: Path, *, dry_run: bool = False, force: bool = False) -> str:
    if yaml is None:
        raise typer.BadParameter("PyYAML is required to write cdt.yaml")
    path = cwd / "cdt.yaml"
    hooks_keep = cwd / "cdt" / "hooks" / ".gitkeep"
    generated = legacy_pipeline_defs()
    actions: list[str] = []

    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise typer.BadParameter("Existing cdt.yaml must contain a mapping")
        if data.get("version") != 1:
            raise typer.BadParameter("Existing cdt.yaml must use version: 1")
        pipelines = data.get("pipelines")
        if not isinstance(pipelines, dict):
            raise typer.BadParameter("Existing cdt.yaml must contain pipelines mapping")
        for name, pipeline in generated.items():
            if name not in pipelines:
                pipelines[name] = pipeline
                actions.append(f"add pipeline {name}")
            elif force:
                pipelines[name] = pipeline
                actions.append(f"overwrite pipeline {name}")
            else:
                actions.append(f"keep existing pipeline {name}")
    else:
        data = {"version": 1, "pipelines": generated}
        actions.append("create cdt.yaml")

    yaml_text = _dump_yaml(data)
    actions.append("ensure cdt/hooks/.gitkeep")
    plan = "\n".join(f"- {action}" for action in actions)
    output = f"Migration plan:\n{plan}\n\nResulting cdt.yaml:\n{yaml_text}"
    if dry_run:
        return output

    if path.exists():
        shutil.copy2(path, cwd / "cdt.yaml.bak")
    path.write_text(yaml_text, encoding="utf-8")
    hooks_keep.parent.mkdir(parents=True, exist_ok=True)
    hooks_keep.touch()
    return output


def _dump_yaml(data: dict[str, Any]) -> str:
    text = yaml.safe_dump(data, sort_keys=False, allow_unicode=True)
    if "prod:" in text and "hook.python_script" not in text:
        text = text.replace(
            "  prod:\n    steps:\n",
            "  prod:\n    steps:\n"
            "    # Uncomment and customize if you need offline data before prod build:\n"
            "    # - hook.python_script:\n"
            "    #     name: fetch_offline_config\n"
            "    #     script: cdt/hooks/fetch_offline_data.py\n"
            "    #     env:\n"
            "    #       OFFLINE_API_URL: ${OFFLINE_API_URL}\n"
            "    #       OFFLINE_OUTPUT: assets/offline_data.json\n"
            "    #     outputs:\n"
            "    #       - assets/offline_data.json\n",
            1,
        )
    return text
