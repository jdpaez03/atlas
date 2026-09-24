"""Export the ATLAS contracts as one JSON Schema (contracts/atlas.schema.json).

The frontend's TypeScript types are generated from this file:
    cd apps/api && uv run python ../../scripts/export_schema.py
    cd apps/web && npm run gen:types
"""

import json
from pathlib import Path

from pydantic.json_schema import models_json_schema

from atlas.core import models as m

ROOT = Path(__file__).resolve().parents[1]
MODELS = [
    m.NodeDefinition, m.DivisionDefinition, m.AgentDefinition, m.AgentState, m.Mission, m.Task, m.AgentMessage, m.AgentReport,
    m.MissionReport, m.ApprovalRequest, m.AtlasEvent, m.Evidence, m.FollowUp, m.EmailDraft, m.Digest, m.Alert, m.RockStatus, m.Brief, m.Audit, m.WorldState,
]

_, schema = models_json_schema([(model, "serialization") for model in MODELS], by_alias=True,
                               ref_template="#/$defs/{model}")
def _strip_field_titles(node, keep: bool = False):
    """Drop per-field titles so the TS generator emits clean inline types.
    Only removes the schema keyword `title` (a string), never a property named 'title'."""
    if isinstance(node, dict):
        if not keep and isinstance(node.get("title"), str):
            node.pop("title")
        for key, value in node.items():
            if key in ("$defs", "properties"):
                for d in value.values():
                    _strip_field_titles(d, keep=(key == "$defs"))
            else:
                _strip_field_titles(value)
    elif isinstance(node, list):
        for item in node:
            _strip_field_titles(item)


_strip_field_titles(schema, keep=True)
schema["title"] = "AtlasContracts"
schema["type"] = "object"
schema["properties"] = {model.__name__: {"$ref": f"#/$defs/{model.__name__}"} for model in MODELS}

out = ROOT / "contracts" / "atlas.schema.json"
out.write_text(json.dumps(schema, indent=2) + "\n", encoding="utf-8")
print(f"wrote {out.relative_to(ROOT)} ({len(schema['$defs'])} definitions)")
