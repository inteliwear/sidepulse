"""Codex memory maintenance must not appear as user agent activity."""
import json
from datetime import datetime, timezone

import pytest

from sidepulse.collector import CODEX_TRANSCRIPT_MAX_LINES, AgentMonitor, LiveAgentMonitor, SourceSpec
from sidepulse.models import AgentMode
from sidepulse.program_adapter import hook_report
from sidepulse.program_presentation import ProgramMonitor
from sidepulse.program_status import ProgramState
from sidepulse.providers import parse_log_line


SESSION_ID = "aaaaaaaa-bbbb-7ccc-8ddd-eeeeeeeeeeee"
MEMORY_PROMPT = "Consolidate the supplied rollout summaries into memory_summary.md"


def write_rollout(root, source, *, padding=0, prompt=MEMORY_PROMPT, session_id=SESSION_ID):
    path = root / f"rollout-2026-11-19T10-16-00-{session_id}.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {"timestamp": now, "type": "session_meta", "payload": {
            "id": session_id, "cwd": "/work/memories_v2", "source": source,
        }},
        *[{"timestamp": now, "type": "event_msg", "payload": {"type": "token_count"}}] * padding,
        {"timestamp": now, "type": "turn_context", "payload": {
            "turn_id": "turn-1", "cwd": "/work/memories_v2",
        }},
        {"timestamp": now, "type": "response_item", "payload": {
            "type": "message", "role": "user",
            "content": [{"type": "input_text", "text": prompt}],
        }},
    ]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


@pytest.mark.parametrize("source", [
    {"internal": "memory_consolidation"},
    {"subagent": "memory_consolidation"},
])
@pytest.mark.parametrize("padding", [0, CODEX_TRANSCRIPT_MAX_LINES + 1])
def test_memory_rollout_does_not_create_agent_activity(tmp_path, source, padding):
    write_rollout(tmp_path, source, padding=padding)
    monitor = AgentMonitor(sources=(SourceSpec("codex-transcripts", tmp_path),))

    snapshot = monitor.snapshot(include_stale=True)

    assert snapshot.statuses == ()
    assert snapshot.stale_statuses == ()
    assert snapshot.aggregate.active_count == 0
    assert snapshot.aggregate.mode == AgentMode.IDLE_READY
    assert snapshot.aggregate.representative is None


@pytest.mark.parametrize("source", [
    {"internal": "memory_consolidation"},
    {"subagent": "memory_consolidation"},
])
def test_memory_metadata_is_independent_of_prompt_and_directory(tmp_path, source):
    path = write_rollout(tmp_path, source, prompt="Changed maintenance prompt")
    path.write_text(path.read_text().replace("/work/memories_v2", "/work/elsewhere"))
    snapshot = AgentMonitor(sources=(SourceSpec("codex-transcripts", tmp_path),)).snapshot()
    assert snapshot.statuses == ()
    assert snapshot.aggregate.active_count == 0


def test_marked_session_stays_hidden_after_hooks_omit_transcript(tmp_path):
    path = write_rollout(tmp_path, {"internal": "memory_consolidation"})
    legacy = LiveAgentMonitor()
    state = ProgramState()
    try:
        for event, fields in [
            ("SessionStart", {}),
            ("UserPromptSubmit", {"prompt": "Perform maintenance"}),
            ("PreToolUse", {"tool_name": "exec_command"}),
            ("PostToolUse", {"tool_name": "exec_command"}),
            ("Stop", {"last_assistant_message": "Memory updated."}),
        ]:
            payload = {"hook_event_name": event, "session_id": SESSION_ID,
                       "cwd": "/work/memories_v2", "transcript_path": str(path) if event == "SessionStart" else None,
                       **fields}
            report = hook_report("codex", payload)
            if report is not None:
                state.submit(report)
            legacy.ingest_record(parse_log_line("codex", json.dumps(payload)))
            for snapshot in (ProgramMonitor(state).snapshot(), legacy.snapshot(include_stale=True)):
                assert snapshot.statuses == (), event
                assert snapshot.stale_statuses == (), event
                assert snapshot.aggregate.active_count == 0, event
                assert snapshot.aggregate.mode == AgentMode.IDLE_READY, event
    finally:
        state.close()


@pytest.mark.parametrize("source", [
    "cli", "vscode", "exec", None,
    {"subagent": {"thread_spawn": {"parent_thread_id": "parent", "depth": 1}}},
    {"internal": "guardian"},
])
def test_user_rollout_with_same_prompt_and_directory_remains_visible(tmp_path, source):
    path = write_rollout(tmp_path, source)
    monitor = AgentMonitor(sources=(SourceSpec("codex-transcripts", tmp_path),))
    snapshot = monitor.snapshot()
    assert snapshot.aggregate.active_count == 1
    assert snapshot.statuses[0].mode == AgentMode.WORKING

    with path.open("a") as stream:
        stream.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "response_item", "payload": {
                "type": "function_call", "name": "exec_command", "arguments": "{}",
            },
        }) + "\n")
    snapshot = monitor.snapshot()
    assert snapshot.aggregate.active_count == 1
    assert snapshot.statuses[0].mode == AgentMode.TOOL_RUNNING
    assert snapshot.statuses[0].tool_name == "exec_command"

    with path.open("a") as stream:
        stream.write(json.dumps({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": "event_msg", "payload": {"type": "task_complete", "last_agent_message": "Done."},
        }) + "\n")
    snapshot = monitor.snapshot()
    assert snapshot.aggregate.active_count == 0
    assert snapshot.statuses[0].mode == AgentMode.COMPLETED


@pytest.mark.parametrize("fields", [
    {},
    {"transcript_path": "/missing/rollout.jsonl"},
    {"transcript_path": "\0"},
])
def test_memory_directory_and_prompt_alone_do_not_hide_hooks(fields):
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": SESSION_ID,
               "cwd": "/work/memories_v2", "transcript_path": None, "prompt": MEMORY_PROMPT,
               **fields}
    assert hook_report("codex", payload)["status"]["state"] == "working"


def test_transcriptless_session_start_in_memory_directory_reports_idle():
    report = hook_report("codex", {
        "hook_event_name": "SessionStart", "session_id": SESSION_ID, "cwd": "/work/memories_v2",
    })
    assert report["status"]["state"] == "idle"


def test_large_valid_header_identifies_memory_session(tmp_path):
    path = write_rollout(tmp_path, {"internal": "memory_consolidation"})
    header, activity = path.read_text().split("\n", 1)
    row = json.loads(header)
    row["payload"]["base_instructions"] = {"text": "x" * 70_000}
    path.write_text(json.dumps(row) + "\n" + activity)
    snapshot = AgentMonitor(sources=(SourceSpec("codex-transcripts", tmp_path),)).snapshot()
    assert snapshot.statuses == ()
    assert snapshot.aggregate.active_count == 0


def test_mixed_rollouts_count_only_user_work(tmp_path):
    for session_id, source in [
        (SESSION_ID, {"internal": "memory_consolidation"}),
        ("bbbbbbbb-bbbb-7ccc-8ddd-eeeeeeeeeeee", {"subagent": "memory_consolidation"}),
        ("cccccccc-bbbb-7ccc-8ddd-eeeeeeeeeeee", "cli"),
    ]:
        write_rollout(tmp_path, source, session_id=session_id)
    snapshot = AgentMonitor(sources=(SourceSpec("codex-transcripts", tmp_path),)).snapshot()
    assert [status.session_id for status in snapshot.statuses] == [session_id]
    assert snapshot.aggregate.active_count == 1
    assert snapshot.aggregate.representative.session_id == session_id


@pytest.mark.parametrize("header", [
    "", "{", "[]", '{"type":"session_meta","payload":null}',
    '{"type":"session_meta","payload":{"source":"unknown"}}',
    json.dumps({"type": "session_meta", "payload": {
        "id": "different-session", "source": {"internal": "memory_consolidation"},
    }}),
])
def test_unusable_header_does_not_hide_user_hooks(tmp_path, header):
    path = tmp_path / f"rollout-{SESSION_ID}.jsonl"
    path.write_text(header + "\n")
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": SESSION_ID,
               "cwd": "/work/memories_v2", "prompt": MEMORY_PROMPT, "transcript_path": str(path)}
    assert hook_report("codex", payload)["status"]["state"] == "working"


def test_header_cache_retries_after_incomplete_file_changes(tmp_path):
    path = tmp_path / f"rollout-2026-11-19T10-16-00-{SESSION_ID}.jsonl"
    path.write_text('{"type":"session_meta"')
    payload = {"hook_event_name": "PreToolUse", "session_id": SESSION_ID,
               "tool_name": "exec_command", "transcript_path": str(path)}
    assert hook_report("codex", payload)["status"]["state"] == "working"
    write_rollout(tmp_path, {"internal": "memory_consolidation"})
    assert hook_report("codex", payload)["status"]["state"] == "clear"


def test_marked_hook_log_suppresses_the_entire_session(tmp_path):
    transcript = write_rollout(tmp_path, {"internal": "memory_consolidation"})
    path = tmp_path / "codex.jsonl"
    events = [
        {"hook_event_name": "SessionStart", "transcript_path": str(transcript)},
        {"hook_event_name": "UserPromptSubmit", "prompt": MEMORY_PROMPT},
        {"hook_event_name": "PreToolUse", "tool_name": "exec_command"},
    ]
    path.write_text("".join(json.dumps({
        "session_id": SESSION_ID, "cwd": "/work/memories_v2", "transcript_path": None, **event,
    }) + "\n" for event in events))
    sources = (SourceSpec("codex", path),)
    snapshot = AgentMonitor(sources=sources).snapshot(include_stale=True)
    assert snapshot.statuses == snapshot.stale_statuses == ()
    assert snapshot.aggregate.active_count == 0

    live = LiveAgentMonitor()
    live.ingest_record(parse_log_line("codex", json.dumps({
        "hook_event_name": "PreToolUse", "session_id": SESSION_ID, "tool_name": "exec_command",
    })))
    assert live.snapshot().aggregate.active_count == 1
    live.recovery_sources = sources
    live.reconcile_recovery_sources()
    snapshot = live.snapshot(include_stale=True)
    assert snapshot.statuses == snapshot.stale_statuses == ()
    assert snapshot.aggregate.active_count == 0
