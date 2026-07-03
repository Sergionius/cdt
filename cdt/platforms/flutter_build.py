from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import typer


def flutter_build_options(
    *,
    dart_defines: Mapping[str, Any] | Sequence[str] | None = None,
    flavor: str | None = None,
    target: str | None = None,
    obfuscate: bool = True,
    split_debug_info: str | None = "obfsymbols",
    no_shrink: bool = False,
    no_pub: bool = True,
    extra_args: Sequence[str] | None = None,
) -> list[str]:
    args: list[str] = []
    if flavor:
        args.extend(["--flavor", flavor])
    if target:
        args.extend(["--target", target])
    if obfuscate:
        args.append("--obfuscate")
    if split_debug_info:
        args.append(f"--split-debug-info={split_debug_info}")
    if no_shrink:
        args.append("--no-shrink")
    args.extend(_dart_define_args(dart_defines))
    if no_pub:
        args.append("--no-pub")
    if extra_args:
        if not all(isinstance(item, str) for item in extra_args):
            raise typer.BadParameter("Flutter build extra_args must be a list of strings")
        args.extend(extra_args)
    return args


def merge_dart_defines(defaults: Mapping[str, Any] | None, overrides: Mapping[str, Any] | Sequence[str] | None):
    if overrides is None:
        return defaults
    if defaults is None:
        return overrides
    if not isinstance(overrides, Mapping):
        return overrides
    merged = dict(defaults)
    merged.update(overrides)
    return merged


def _dart_define_args(dart_defines: Mapping[str, Any] | Sequence[str] | None) -> list[str]:
    if dart_defines is None:
        return []
    if isinstance(dart_defines, Mapping):
        return [f"--dart-define={key}={value}" for key, value in dart_defines.items()]
    if isinstance(dart_defines, str) or not all(isinstance(item, str) for item in dart_defines):
        raise typer.BadParameter("Flutter build dart_defines must be a mapping or a list of strings")
    return [item if item.startswith("--dart-define=") else f"--dart-define={item}" for item in dart_defines]
