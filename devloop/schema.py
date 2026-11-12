"""Structured-output schemas shared by every agent CLI, plus a strict validator.

The schemas use only the JSON Schema subset that all three CLIs accept for
structured output (codex requires every property to be required and
``additionalProperties: false``). The validator covers exactly that subset, so
devloop stays free of third-party dependencies.
"""

import json
from pathlib import Path

SCHEMA_DIR = Path(__file__).resolve().parent / 'schemas'
SCHEMA_NAMES = ('implement', 'fix', 'review')


class SchemaError(ValueError):
    pass


def schema_path(name):
    if name not in SCHEMA_NAMES:
        raise KeyError(name)
    return SCHEMA_DIR / f'{name}.json'


def load_schema(name):
    return json.loads(schema_path(name).read_text(encoding='utf-8'))


_TYPES = {
    'object': lambda v: isinstance(v, dict),
    'array': lambda v: isinstance(v, list),
    'string': lambda v: isinstance(v, str),
    # bool is a subclass of int in Python; JSON booleans are not integers.
    'integer': lambda v: isinstance(v, int) and not isinstance(v, bool),
}


def validate(value, schema, path='$'):
    """Raise SchemaError unless ``value`` matches ``schema`` exactly."""
    kind = schema.get('type')
    if kind not in _TYPES:
        raise SchemaError(f'{path}: unsupported schema type {kind!r}')
    if not _TYPES[kind](value):
        raise SchemaError(f'{path}: expected {kind}')
    if 'enum' in schema and value not in schema['enum']:
        raise SchemaError(f'{path}: {value!r} not in {schema["enum"]}')
    if kind == 'object':
        props = schema.get('properties', {})
        missing = [k for k in schema.get('required', ()) if k not in value]
        if missing:
            raise SchemaError(f'{path}: missing {", ".join(missing)}')
        if schema.get('additionalProperties') is False:
            extra = sorted(set(value) - set(props))
            if extra:
                raise SchemaError(f'{path}: unexpected {", ".join(extra)}')
        for key, sub in props.items():
            if key in value:
                validate(value[key], sub, f'{path}.{key}')
    elif kind == 'array':
        for i, item in enumerate(value):
            validate(item, schema['items'], f'{path}[{i}]')
    return value
