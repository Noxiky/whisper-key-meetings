import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = ROOT / "schemas"
GOLDEN = ROOT / "fixtures" / "golden" / "learning-session-v1"


@pytest.fixture
def schema_validator():
    def build(name):
        schema = json.loads((SCHEMAS / name).read_text(encoding="utf-8"))
        resources = []
        for schema_path in SCHEMAS.glob("*.schema.json"):
            document = json.loads(schema_path.read_text(encoding="utf-8"))
            resources.append((document["$id"], Resource.from_contents(document)))
        registry = Registry().with_resources(resources)
        return Draft202012Validator(schema, registry=registry, format_checker=FormatChecker())

    return build
