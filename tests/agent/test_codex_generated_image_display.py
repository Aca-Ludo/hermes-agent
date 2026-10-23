"""The native Codex bridge must never display encoded pixels or upstream errors."""

import json
from types import SimpleNamespace

import pytest

from agent.codex_runtime import make_codex_app_server_event_bridge
from agent.transports.codex_event_projector import CodexEventProjector


@pytest.mark.parametrize("result", [
    "private-encoded-image", {"success": False, "error": "private-upstream-error"},
    {"success": True, "image": "/image.png", "extra": "private-payload"},
])
def test_image_completion_only_exposes_normalized_card_fields(result):
    completed = []
    agent = SimpleNamespace(tool_complete_callback=lambda *args: completed.append(args))
    note = {"method": "item/completed", "params": {"item": {
        "type": "imageGeneration", "id": "img", "result": result,
    }}}
    make_codex_app_server_event_bridge(agent)(note)
    projection = CodexEventProjector().project(note)
    assert projection.is_tool_iteration
    call = projection.messages[0]["tool_calls"][0]
    assert completed[0][0] == call["id"]
    assert completed[0][1:3] == ("image_generate", {})
    assert completed[0][3] == projection.messages[1]["content"]
    assert "private-" not in json.dumps([completed, projection.messages])
    assert "extra" not in json.loads(completed[0][3])
