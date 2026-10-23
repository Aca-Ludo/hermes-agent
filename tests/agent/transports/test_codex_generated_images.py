"""Generated Codex image deliverables must survive both live display and replay."""

import base64
import copy
import io
import json
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
import pytest

from agent.codex_runtime import make_codex_app_server_event_bridge
from agent.transports.codex_app_server_session import CodexAppServerSession, TurnResult
from agent.transports.codex_event_projector import CodexEventProjector


def image_note(format="PNG"):
    buffer = io.BytesIO()
    Image.new("RGB", (3, 2), "red").save(buffer, format=format)
    payload = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"method": "item/completed", "params": {"item": {
        "type": "imageGeneration", "id": "image-1", "status": "completed", "result": payload,
    }}}


def recording_session(tmp_path):
    completed, started, progress, notes = [], [], [], []
    agent = SimpleNamespace(
        tool_start_callback=lambda *args: started.append(args),
        tool_complete_callback=lambda *args: completed.append(args),
        tool_progress_callback=lambda *args, **kwargs: progress.append((args, kwargs)),
    )
    bridge = make_codex_app_server_event_bridge(agent)

    def on_event(note):
        notes.append(note)
        bridge(note)

    session = CodexAppServerSession(cwd=str(tmp_path), on_event=on_event)
    return session, completed, started, progress, notes


def test_completed_image_has_one_durable_file_shared_by_live_card_and_history(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    session, completed, started, progress, notes = recording_session(tmp_path)
    note = image_note()
    original = copy.deepcopy(note)
    projector, result = CodexEventProjector(), TurnResult()
    session._absorb_notification(result, projector, {"method": "item/started", "params": {
        "item": {"type": "imageGeneration", "id": "image-1", "status": "inProgress"}}})
    projection, aborted = session._absorb_notification(result, projector, note)

    assert projection.is_tool_iteration
    assert not aborted
    assert result.tool_iterations == 1
    assistant, tool = result.projected_messages
    call = assistant["tool_calls"][0]
    assert call["function"] == {"name": "image_generate", "arguments": "{}"}
    assert completed[0][:3] == started[0] == (call["id"], "image_generate", {})
    assert completed[0][3] == tool["content"]
    payload = json.loads(tool["content"])
    assert payload["success"] is True
    path = Path(payload["image"])
    assert path.is_absolute()
    assert path.parent == home / "cache" / "generated" / "images"
    with Image.open(path) as image:
        assert image.format == "PNG"
        assert image.size == (3, 2)
    assert list(path.parent.iterdir()) == [path]
    assert progress[-1][1]["is_error"] is False
    assert note == original  # no mutation of the provider's source event
    assert original["params"]["item"]["result"] not in json.dumps([notes, result.projected_messages, completed])
    # A persisted/reloaded tool result uses the very same deliverable, not another save.
    replay = json.loads(json.dumps(result.projected_messages))
    assert json.loads(replay[1]["content"])["image"] == str(path)
    assert projector.project(notes[-1]).messages == projection.messages
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize("case", [
    "invalid-base64", "whitespace", "data-url", "missing", "number", "forged-result",
    "gif", "bmp", "truncated", "failed", "canceled", "inProgress", "error",
])
def test_invalid_image_is_a_small_safe_failed_card_without_files(tmp_path, monkeypatch, case):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    note = image_note("GIF" if case == "gif" else "BMP" if case == "bmp" else "PNG")
    item = note["params"]["item"]
    payload = item["result"]
    replacements = {
        "invalid-base64": "private-invalid-payload!",
        "whitespace": payload + "\n",
        "data-url": "data:image/png;base64," + payload,
        "missing": None,
        "number": 123,
        "forged-result": {"success": True, "image": "/private-image.png", "secret": payload},
        "truncated": base64.b64encode(base64.b64decode(payload)[:30]).decode(),
    }
    if case in replacements:
        item["result"] = replacements[case]
    if case in {"failed", "canceled", "inProgress"}:
        item["status"] = case
    if case == "error":
        item["error"] = {"message": "private-upstream-error " + payload}
    session, completed, _, progress, notes = recording_session(tmp_path)
    result = TurnResult()
    projection, _ = session._absorb_notification(result, CodexEventProjector(), note)
    assert projection.is_tool_iteration
    output = json.loads(completed[0][3])
    assert output["success"] is False
    assert "image" not in output
    assert progress[-1][1]["is_error"] is True
    assert completed[0][3] == result.projected_messages[1]["content"]
    assert len(completed[0][3]) < 200
    rendered = json.dumps([notes, completed, result.projected_messages])
    assert payload not in rendered
    assert "private-" not in rendered
    assert not list(home.rglob("codex-image-*"))


@pytest.mark.parametrize("limit,value", [
    ("MAX_ENCODED_IMAGE_CHARS", 8), ("MAX_IMAGE_BYTES", 8), ("MAX_IMAGE_PIXELS", 5),
])
def test_image_resource_limits_fail_closed(tmp_path, monkeypatch, limit, value):
    from agent.transports import codex_generated_images as media

    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(media, limit, value, raising=False)
    note = image_note()
    if limit == "MAX_ENCODED_IMAGE_CHARS":
        def no_decode(*args, **kwargs):
            pytest.fail("oversized encoded input must be rejected before decoding")
        monkeypatch.setattr(media.base64, "b64decode", no_decode)
    session, completed, _, _, _ = recording_session(tmp_path)
    session._absorb_notification(TurnResult(), CodexEventProjector(), note)
    assert json.loads(completed[0][3])["success"] is False
    assert not list(home.rglob("codex-image-*"))


@pytest.mark.parametrize("case", ["truncated-jpeg", "decompression-bomb", "bad-png-checksum"])
def test_corrupt_image_or_decoder_bomb_does_not_escape_session(tmp_path, monkeypatch, case):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    note = image_note("JPEG" if case == "truncated-jpeg" else "PNG")
    if case == "truncated-jpeg":
        raw = base64.b64decode(note["params"]["item"]["result"])
        note["params"]["item"]["result"] = base64.b64encode(raw[:-2]).decode()
    elif case == "decompression-bomb":
        monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1)
    else:
        raw = bytearray(base64.b64decode(note["params"]["item"]["result"]))
        raw[raw.index(b"IDAT") + 4] ^= 1
        note["params"]["item"]["result"] = base64.b64encode(raw).decode()
    session, completed, _, _, _ = recording_session(tmp_path)
    session._absorb_notification(TurnResult(), CodexEventProjector(), note)
    assert json.loads(completed[0][3])["success"] is False
    assert not list(home.rglob("codex-image-*"))


def test_failed_file_write_removes_partial_image_without_error_detail_leak(tmp_path, monkeypatch):
    from agent.transports import codex_generated_images as media

    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    real_fdopen = media.os.fdopen

    class BrokenOutput:
        def __init__(self, fd, mode):
            self.file = real_fdopen(fd, mode)

        def __enter__(self):
            return self

        def write(self, raw):
            self.file.write(raw[:8])
            raise OSError("private-filesystem-error")

        def __exit__(self, *args):
            self.file.close()

    monkeypatch.setattr(media.os, "fdopen", BrokenOutput)
    session, completed, _, _, notes = recording_session(tmp_path)
    session._absorb_notification(TurnResult(), CodexEventProjector(), image_note())
    assert json.loads(completed[0][3])["success"] is False
    assert "private-filesystem-error" not in json.dumps([completed, notes])
    assert not list(home.rglob("codex-image-*"))


def test_started_image_drops_unexpected_payload_without_writing(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    note = image_note()
    note["method"] = "item/started"
    note["params"]["item"]["error"] = "private-error"
    session, completed, started, _, notes = recording_session(tmp_path)
    projection, _ = session._absorb_notification(TurnResult(), CodexEventProjector(), note)
    assert not projection.messages
    assert not completed
    assert started[0][1:] == ("image_generate", {})
    assert note["params"]["item"]["result"] not in json.dumps(notes)
    assert "private-error" not in json.dumps(notes)
    assert not list(home.rglob("codex-image-*"))


@pytest.mark.parametrize("format,extension", [("JPEG", ".jpg"), ("WEBP", ".webp")])
def test_supported_image_formats_keep_correct_extension(tmp_path, monkeypatch, format, extension):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    session, completed, _, _, _ = recording_session(tmp_path)
    session._absorb_notification(TurnResult(), CodexEventProjector(), image_note(format))
    payload = json.loads(completed[0][3])
    assert payload["success"] is True
    path = Path(payload["image"])
    assert path.suffix == extension
    with Image.open(path) as image:
        image.load()
        assert image.format == format


@pytest.mark.platforms("posix")
def test_generated_file_is_private_on_posix(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile"))
    session, completed, _, _, _ = recording_session(tmp_path)
    session._absorb_notification(TurnResult(), CodexEventProjector(), image_note())
    path = Path(json.loads(completed[0][3])["image"])
    assert path.stat().st_mode & 0o777 == 0o600


def test_image_uses_context_profile_not_process_launch_home(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    launch_home, owning_home = tmp_path / "launch", tmp_path / "owner"
    monkeypatch.setenv("HERMES_HOME", str(launch_home))
    session, completed, _, _, _ = recording_session(tmp_path)
    token = set_hermes_home_override(owning_home)
    try:
        session._absorb_notification(TurnResult(), CodexEventProjector(), image_note())
    finally:
        reset_hermes_home_override(token)
    path = Path(json.loads(completed[0][3])["image"])
    assert path.is_relative_to(owning_home)
    assert not launch_home.exists()


class QueuedClient:
    """Only the transport boundary is fake; session filtering, projection and I/O are real."""

    def __init__(self, notes):
        self.notes = list(notes)

    def request(self, method, params=None, timeout=None):
        assert method == "turn/start"
        return {"turn": {"id": "turn"}}

    def is_alive(self):
        return True

    def take_server_request(self, timeout=0):
        return None

    def take_notification(self, timeout=0):
        assert self.notes, "session consumed every fixture event without ending the turn"
        return self.notes.pop(0)


def test_session_filters_foreign_images_before_io_preserves_reasoning_and_final(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    session, completed, _, _, notes = recording_session(tmp_path)
    foreign_thread, foreign_turn, local = image_note(), image_note(), image_note()
    foreign_thread["params"].update(threadId="foreign", turnId="turn")
    foreign_turn["params"].update(threadId="thread", turnId="foreign")
    local["params"].update(threadId="thread", turnId="turn")
    session._thread_id = "thread"
    session._client = QueuedClient([
        foreign_thread, foreign_turn,
        {"method": "item/completed", "params": {"item": {"type": "reasoning", "summary": ["Image reasoning"]}}},
        local,
        {"method": "item/completed", "params": {"item": {"type": "reasoning", "summary": ["Final reasoning"]}}},
        {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "Here is your image."}}},
        {"method": "turn/completed", "params": {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}}},
    ])
    result = session.run_turn("Generate an image", turn_timeout=5)
    assert result.error is None
    assert not result.interrupted
    assert result.tool_iterations == 1
    assert result.final_text == "Here is your image."
    assert [m["role"] for m in result.projected_messages] == ["assistant", "tool", "assistant"]
    assert result.projected_messages[0]["reasoning"] == "Image reasoning"
    assert result.projected_messages[-1]["reasoning"] == "Final reasoning"
    assert len(completed) == 1
    assert len(list(home.rglob("codex-image-*"))) == 1
    assert not any(n["params"].get("threadId") == "foreign" or n["params"].get("turnId") == "foreign" for n in notes)


def test_image_result_reloads_from_real_session_database_without_new_file(tmp_path, monkeypatch):
    from hermes_state import SessionDB

    home = tmp_path / "profile"
    monkeypatch.setenv("HERMES_HOME", str(home))
    session, completed, _, _, _ = recording_session(tmp_path)
    result = TurnResult()
    session._absorb_notification(result, CodexEventProjector(), image_note())
    path = Path(json.loads(completed[0][3])["image"])
    db_path = home / "state.db"
    db = SessionDB(db_path)
    try:
        db.create_session(session_id="image-session", source="desktop", model="codex")
        for message in result.projected_messages:
            db.append_message("image-session", **message)
    finally:
        db.close()
    db = SessionDB(db_path)
    try:
        rows = db.get_messages("image-session")
        assert [row["role"] for row in rows] == ["assistant", "tool"]
        assert rows[1]["tool_call_id"] == completed[0][0]
        assert rows[1]["content"] == completed[0][3]
        assert json.loads(rows[1]["content"])["image"] == str(path)
        assert path.is_file()
        assert list(path.parent.iterdir()) == [path]
    finally:
        db.close()
