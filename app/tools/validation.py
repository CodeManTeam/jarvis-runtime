from __future__ import annotations

from typing import Any


class SchemaValidationError(ValueError):
    """Raised for a basic JSON Schema validation failure."""

    def __init__(self, message: str, path: str = "$") -> None:
        self.path = path
        super().__init__(f"{path}: {message}")


def validate_input(value: Any, schema: dict[str, Any]) -> None:
    """Validate the JSON Schema subset needed by tool inputs.

    Supported keywords are intentionally small and predictable: type,
    required, properties, additionalProperties, enum, items, minItems,
    maxItems, minimum, maximum, minLength, and maxLength.
    """

    if not isinstance(schema, dict):
        raise SchemaValidationError("schema must be an object")
    _validate(value, schema, "$")


def _validate(value: Any, schema: dict[str, Any], path: str) -> None:
    if "enum" in schema and value not in schema["enum"]:
        raise SchemaValidationError(
            f"value must be one of {schema['enum']!r}",
            path,
        )

    expected = schema.get("type")
    if expected and not _matches_type(value, expected):
        raise SchemaValidationError(
            f"expected type {expected!r}, got {_type_name(value)!r}",
            path,
        )

    if isinstance(value, dict):
        required = schema.get("required", [])
        if not isinstance(required, list):
            raise SchemaValidationError("required must be an array", path)
        for key in required:
            if key not in value:
                raise SchemaValidationError(f"missing required property {key!r}", path)

        properties = schema.get("properties", {})
        if not isinstance(properties, dict):
            raise SchemaValidationError("properties must be an object", path)
        for key, child_schema in properties.items():
            if key in value:
                _validate(value[key], child_schema, f"{path}.{key}")

        if schema.get("additionalProperties") is False:
            unknown = sorted(set(value) - set(properties))
            if unknown:
                raise SchemaValidationError(
                    f"additional properties are not allowed: {unknown!r}",
                    path,
                )

    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            raise SchemaValidationError(
                f"must contain at least {schema['minItems']} items",
                path,
            )
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise SchemaValidationError(
                f"must contain at most {schema['maxItems']} items",
                path,
            )
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(value):
                _validate(item, item_schema, f"{path}[{index}]")

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            raise SchemaValidationError(f"must be >= {schema['minimum']}", path)
        if "maximum" in schema and value > schema["maximum"]:
            raise SchemaValidationError(f"must be <= {schema['maximum']}", path)

    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            raise SchemaValidationError(
                f"must contain at least {schema['minLength']} characters",
                path,
            )
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            raise SchemaValidationError(
                f"must contain at most {schema['maxLength']} characters",
                path,
            )


def _matches_type(value: Any, expected: str | list[str]) -> bool:
    expected_types = [expected] if isinstance(expected, str) else expected
    return any(
        {
            "object": isinstance(value, dict),
            "array": isinstance(value, list),
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool),
            "integer": isinstance(value, int) and not isinstance(value, bool),
            "boolean": isinstance(value, bool),
            "null": value is None,
        }.get(type_name, False)
        for type_name in expected_types
    )


def _type_name(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, dict):
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    return type(value).__name__
