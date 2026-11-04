"""Unit tests for output plumbing, breakpoint tracking and command builders."""

import importlib
import os
import queue
import threading
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import mcp_pdb.main as m
else:  # mcp_pdb/__init__ re-exports main(), shadowing the submodule attribute
    m = importlib.import_module("mcp_pdb.main")


@pytest.fixture(autouse=True)
def _isolate_state():
    saved = (dict(m.breakpoints), m.current_project_root, m.caveman_mode, m.remote_mode)
    m.breakpoints.clear()
    m._drain_queue()
    yield
    m.breakpoints.clear()
    m.breakpoints.update(saved[0])
    m.current_project_root, m.caveman_mode, m.remote_mode = saved[1:]
    m._drain_queue()


def _pump(chunks, more_pending=None):
    q: queue.Queue[str] = queue.Queue()
    it = iter(chunks)
    eof = threading.Event()
    m._pump_stream(lambda: next(it, b""), more_pending, q, eof, "test")
    assert eof.is_set()
    out = []
    while not q.empty():
        out.append(q.get_nowait())
    return out


class TestPumpStream:
    def test_prompt_without_newline_is_emitted_immediately(self):
        """Even without select() (Windows pipes), the prompt must not wait."""
        emitted_before_eof = []

        def recv_factory():
            chunks = iter([b"> f.py(1)<module>()\n-> x = 1\n(Pdb) "])

            def recv():
                chunk = next(chunks, None)
                if chunk is None:
                    emitted_before_eof.extend(list(q.queue))
                    return b""
                return chunk

            return recv

        q: queue.Queue[str] = queue.Queue()
        m._pump_stream(recv_factory(), None, q, None, "test")
        assert emitted_before_eof[-1] == "(Pdb) "

    def test_partial_lines_are_joined_across_chunks(self):
        assert _pump([b"hel", b"lo\nwor", b"ld\n"]) == ["hello", "world"]

    def test_non_prompt_residue_held_while_more_data_pending(self):
        assert _pump([b"abc", b"def\n"], more_pending=lambda: True) == ["abcdef"]

    def test_non_prompt_residue_emitted_when_idle(self):
        """An input() prompt from user code is surfaced when the stream goes quiet."""
        assert _pump([b"Enter name: "], more_pending=lambda: False) == ["Enter name: "]

    def test_nested_debugger_prompt_detected(self):
        assert m._is_prompt("((Pdb)) ")
        assert m._is_prompt("(Pdb) ")
        assert not m._is_prompt("value (Pdb) inside more")


class TestCollectOutput:
    def test_stops_at_prompt_and_reports_it(self):
        for line in ("a", "(Pdb) ", "leftover"):
            m.pdb_output_queue.put(line)
        lines, saw_prompt = m._collect_output(1.0)
        assert lines == ["a", "(Pdb) "]
        assert saw_prompt
        assert m._drain_queue() == ["leftover"]

    def test_returns_early_on_eof(self, monkeypatch):
        eof = threading.Event()
        eof.set()
        monkeypatch.setattr(m, "stream_eof", eof)
        m.pdb_output_queue.put("bye")
        lines, saw_prompt = m._collect_output(5.0)
        assert lines == ["bye"]
        assert not saw_prompt


class TestBreakpointSync:
    def test_tracks_set_and_delete_from_pdb_output(self, tmp_path):
        m.current_project_root = str(tmp_path)
        path = str(tmp_path / "mod.py")
        m._sync_breakpoints(f"Breakpoint 3 at {path}:12\n(Pdb) ")
        assert m.breakpoints[path][12]["bp_number"] == "3"
        assert m.breakpoints[path][12]["project_root"] == str(tmp_path)

        m._sync_breakpoints(f"Deleted breakpoint 3 at {path}:12")
        assert path not in m.breakpoints

    def test_reset_keeps_condition(self, tmp_path):
        path = str(tmp_path / "mod.py")
        m.breakpoints[path] = {5: {"bp_number": "1", "condition": "x > 1"}}
        m._sync_breakpoints(f"Breakpoint 7 at {path}:5")
        assert m.breakpoints[path][5] == {
            "bp_number": "7",
            "condition": "x > 1",
            "project_root": m.current_project_root,
        }

    def test_ignored_for_remote_sessions(self, tmp_path):
        m.remote_mode = True
        m._sync_breakpoints(f"Breakpoint 1 at {tmp_path}/x.py:1")
        assert m.breakpoints == {}


class TestFormatting:
    def test_truncate_keeps_head_and_tail(self):
        text = "H" * 5000 + "T" * 5000
        out = m._truncate(text, limit=1000)
        assert out.startswith("H" * 600)
        assert out.endswith("T" * 300)
        assert "characters omitted" in out

    def test_truncate_noop_when_short(self):
        assert m._truncate("short", limit=100) == "short"

    def test_command_word(self):
        assert m._command_word("  UNT 42") == "unt"
        assert m._command_word("") == ""


class TestExamineCommand:
    @pytest.mark.parametrize("pretty", [True, False])
    @pytest.mark.parametrize("attrs", [True, False])
    def test_generated_code_runs_and_leaves_no_names(self, pretty, attrs, capsys):
        cmd = m._examine_command("data['k']", pretty=pretty, attrs=attrs, max_chars=100)
        assert cmd.startswith("!")
        frame_ns = {"data": {"k": list(range(200))}}
        before = set(frame_ns)
        exec(compile(cmd[1:], "<pdb>", "single"), frame_ns)
        out = capsys.readouterr().out
        assert "Type: list" in out
        assert "Len: 200" in out
        assert "truncated" in out
        assert ("Attrs:" in out) == attrs
        assert set(frame_ns) - {"__builtins__"} == before

    def test_repr_failure_is_reported(self, capsys):
        class Bad:
            def __repr__(self):
                raise RuntimeError("boom")

        cmd = m._examine_command("obj", pretty=True, attrs=False, max_chars=100)
        exec(compile(cmd[1:], "<pdb>", "single"), {"obj": Bad()})
        assert "repr() failed" in capsys.readouterr().out


def test_module_never_prints():
    """stdout is the MCP JSON-RPC channel; main.py must log to stderr only."""
    import ast

    with open(os.path.join(os.path.dirname(m.__file__), "main.py")) as f:
        tree = ast.parse(f.read())
    prints = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "print"
    ]
    assert prints == []
