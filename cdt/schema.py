from __future__ import annotations

import dataclasses
import inspect
import json
import types
from pathlib import Path
from typing import Any, get_args, get_origin, get_type_hints

from .pipeline.builtins import _BUILTINS

SCHEMA_ID = "https://raw.githubusercontent.com/Sergionius/cdt/main/cdt/cdt.schema.json"


def schema_payload() -> dict[str, Any]:
    step_names = sorted(_BUILTINS)
    step_objects = [_step_object_schema(name, step_class) for name, step_class in sorted(_BUILTINS.items())]
    step_objects.append(
        {
            "type": "object",
            "description": "Project plugin step",
            "minProperties": 1,
            "maxProperties": 1,
            "propertyNames": {"pattern": r"^(?!parallel$|sequence$)[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)+$"},
            "additionalProperties": {"type": ["object", "null"]},
        }
    )
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": SCHEMA_ID,
        "title": "CDT pipeline configuration",
        "type": "object",
        "additionalProperties": False,
        "required": ["version", "pipelines"],
        "properties": {
            "version": {"const": 1},
            "plugins": {"type": "array", "items": {"type": "string"}, "default": []},
            "pipelines": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {"$ref": "#/$defs/pipeline"},
            },
        },
        "$defs": {
            "pipeline": {
                "type": "object",
                "additionalProperties": False,
                "required": ["steps"],
                "properties": {
                    "risk": {"enum": ["standard", "production"], "default": "standard"},
                    "steps": {"type": "array", "items": {"$ref": "#/$defs/item"}},
                },
            },
            "item": {
                "oneOf": [
                    {"type": "string", "enum": step_names},
                    *step_objects,
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["parallel"],
                        "properties": {
                            "parallel": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["steps"],
                                "properties": {
                                    "steps": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/parallelItem"}}
                                },
                            }
                        },
                    },
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["sequence"],
                        "properties": {
                            "sequence": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["steps"],
                                "properties": {
                                    "steps": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/step"}}
                                },
                            }
                        },
                    },
                ]
            },
            "step": {"oneOf": [{"type": "string", "enum": step_names}, *step_objects]},
            "parallelItem": {
                "oneOf": [
                    {"$ref": "#/$defs/step"},
                    {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["sequence"],
                        "properties": {
                            "sequence": {
                                "type": "object",
                                "additionalProperties": False,
                                "required": ["steps"],
                                "properties": {
                                    "steps": {"type": "array", "minItems": 1, "items": {"$ref": "#/$defs/step"}}
                                },
                            }
                        },
                    },
                ]
            },
        },
    }


def write_schema(path: Path) -> None:
    path.write_text(json.dumps(schema_payload(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def bundled_schema_path() -> Path:
    return Path(__file__).with_name("cdt.schema.json")


def _step_object_schema(name: str, step_class: type) -> dict[str, Any]:
    properties: dict[str, Any] = {}
    required: list[str] = []
    if dataclasses.is_dataclass(step_class):
        hints = get_type_hints(step_class)
        for field in dataclasses.fields(step_class):
            properties[field.name] = _annotation_schema(hints.get(field.name, Any))
            if field.default is dataclasses.MISSING and field.default_factory is dataclasses.MISSING:
                required.append(field.name)
    else:
        signature = inspect.signature(step_class.__init__)
        try:
            hints = get_type_hints(step_class.__init__)
        except (NameError, TypeError):
            hints = {}
        for parameter in signature.parameters.values():
            if parameter.name == "self" or parameter.kind in {parameter.VAR_POSITIONAL, parameter.VAR_KEYWORD}:
                continue
            properties[parameter.name] = _annotation_schema(hints.get(parameter.name, Any))
            if parameter.default is inspect.Parameter.empty:
                required.append(parameter.name)
    options: dict[str, Any] = {
        "type": ["object", "null"],
        "additionalProperties": False,
        "properties": properties,
    }
    if required:
        options["required"] = required
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [name],
        "properties": {name: options},
    }


def _annotation_schema(annotation: Any) -> dict[str, Any]:
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin in (types.UnionType, getattr(__import__("typing"), "Union", object)):
        schemas = [_annotation_schema(arg) for arg in args]
        types_found = [schema.get("type") for schema in schemas if isinstance(schema.get("type"), str)]
        if len(types_found) == len(schemas):
            return {"type": types_found}
        return {"anyOf": schemas}
    if origin in (list, tuple, set):
        return {"type": "array", "items": _annotation_schema(args[0] if args else Any)}
    if origin is dict:
        return {"type": "object", "additionalProperties": _annotation_schema(args[1] if len(args) > 1 else Any)}
    if annotation is bool:
        return {"type": "boolean"}
    if annotation is int:
        return {"type": "integer"}
    if annotation is float:
        return {"type": "number"}
    if annotation in (str, Path):
        return {"type": "string"}
    if annotation is type(None):
        return {"type": "null"}
    return {}
