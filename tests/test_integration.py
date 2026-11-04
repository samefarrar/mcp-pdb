"""End-to-end tests that drive real pdb processes through the tool functions."""

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    import mcp_pdb.main as m
else:  # mcp_pdb/__init__ re-exports main(), shadowing the submodule attribute
    m = importlib.import_module("mcp_pdb.main")

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX signals")


@pytest.fixture
def session():
    m.breakpoints.clear()
    yield m
    m.end_debug()
    m.breakpoints.clear()


@pytest.fixture
def project(tmp_path):
    """A non-uv project (requirements.txt marks the root) with two modules."""
    (tmp_path / "requirements.txt").touch()
    (tmp_path / "helper.py").write_text(
        textwrap.dedent(
            """\
            def double(v):
                out = v * 2
                return out
            """
        )
    )
    (tmp_path / "app.py").write_text(
        textwrap.dedent(
            """\
            import time
            from helper import double

            data = {"a": [1, 2, 3]}
            for i in range(5):
                y = double(i)
            time.sleep(2)
            print("slept")
            z = 1
            """
        )
    )
    return tmp_path


def _location(output: str) -> str:
    """'file.py(line)' of the last '> ...' location line in pdb output."""
    found = re.findall(r"^> .*?([^/\\]+\.py\(\d+\))", output, re.M)
    return found[-1] if found else ""


def test_commands_return_as_soon_as_prompt_appears(session, project):
    out = session.start_debug(str(project / "app.py"))
    assert "app.py(1)" in out
    start = time.monotonic()
    out = session.send_pdb_command("n")
    assert time.monotonic() - start < 1.0, "should not wait for a timeout"
    assert _location(out) == "app.py(2)"
    assert "-- Current location --" in out


def test_late_output_is_returned_not_dropped(session, project):
    session.start_debug(str(project / "app.py"))
    session.send_pdb_command("unt 7")
    out = session.send_pdb_command("n", timeout=0.3)  # time.sleep(2) is running
    assert "still running" in out
    out = session.get_output(timeout=10)
    assert "[At (Pdb) prompt]" in out
    out = session.send_pdb_command("n")
    assert "slept" in out


def test_breakpoints_in_other_files_survive_restart_with_condition(session, project):
    session.start_debug(str(project / "app.py"), caveman_mode=True)
    assert "Breakpoint #1" in session.set_breakpoint("helper.py", 2, condition="v == 3")
    session.send_pdb_command("b app.py:9")  # raw command is tracked too

    out = session.restart_debug()
    assert "helper.py:2: Breakpoint" in out
    assert "app.py:9: Breakpoint" in out
    assert session.caveman_mode is True, "restart must preserve caveman_mode"

    out = session.send_pdb_command("c")
    assert _location(out) == "helper.py(2)"
    assert session.send_pdb_command("p v").strip().endswith("3")


def test_clear_breakpoint_reports_truthfully(session, project):
    session.start_debug(str(project / "app.py"))
    assert "No breakpoint cleared" in session.clear_breakpoint("app.py", 9)
    session.set_breakpoint("app.py", 9)
    assert "Breakpoint cleared" in session.clear_breakpoint("app.py", 9)
    assert m.breakpoints == {}


def test_examine_variable_single_round_trip_and_no_pollution(session, project):
    session.start_debug(str(project / "app.py"))
    session.send_pdb_command("unt 5")
    out = session.examine_variable("data")
    assert "Type: dict" in out
    assert "Len: 1" in out
    assert "Attrs:" in out
    names = session.send_pdb_command("p sorted(k for k in dir() if not k.startswith('__'))")
    assert "'data'" in names
    assert "_v" not in names
    assert "inspect" not in names
    assert "NameError" in session.examine_variable("missing_name")


def test_status_does_not_send_commands_while_program_runs(session, project):
    session.start_debug(str(project / "app.py"))
    session.send_pdb_command("unt 7")
    session.send_pdb_command("n", timeout=0.2)
    assert "Program is running" in session.get_debug_status()


@posix_only
def test_interrupt_stops_in_user_code_even_with_breakpoints(session, project):
    session.start_debug(str(project / "app.py"))
    session.set_breakpoint("helper.py", 2, condition="v == 3")
    session.send_pdb_command("c")
    session.send_pdb_command("c", timeout=0.3)  # now inside time.sleep(2)
    out = session.interrupt()
    assert "Program interrupted" in out
    # Not bdb.py / pdb.py / linecache.py internals.  (With breakpoints active
    # pdb stops at the next user line event, i.e. once sleep() resumes.)
    assert _location(out).startswith("app.py("), out


def test_session_end_is_detected(session, project):
    session.start_debug(str(project / "app.py"))
    session.send_pdb_command("q")
    assert session.pdb_running is False
    assert "No active debugging session" in session.send_pdb_command("n")


def test_pytest_mode_stops_at_failure(session, tmp_path):
    (tmp_path / "requirements.txt").touch()
    (tmp_path / "test_x.py").write_text("def test_fail():\n    value = 41\n    assert value == 42\n")
    out = session.start_debug(str(tmp_path / "test_x.py"), use_pytest=True)
    assert "(Pdb)" in out
    assert session.send_pdb_command("p value").strip().splitlines()[1] == "41"


def test_pytest_mode_all_passing_explains(session, tmp_path):
    (tmp_path / "requirements.txt").touch()
    (tmp_path / "test_ok.py").write_text("def test_ok():\n    assert True\n")
    out = session.start_debug(str(tmp_path / "test_ok.py"), use_pytest=True)
    assert "exited before reaching a prompt" in out
    assert "nothing to debug" in out


@posix_only
@pytest.mark.skipif(shutil.which("uv") is None, reason="uv not installed")
def test_uv_project_end_kills_whole_process_group(session, tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0"\nrequires-python = ">=3.10"\ndependencies = []\n'
    )
    (tmp_path / "app.py").write_text("import time\nwhile True:\n    time.sleep(0.1)\n")
    out = session.start_debug(str(tmp_path / "app.py"))
    assert "app.py(1)" in out, out
    pgid = session.pdb_process.pid
    session.send_pdb_command("c", timeout=0.3)
    out = session.interrupt()
    assert "Program interrupted" in out
    session.end_debug()
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_remote_session_eof_is_detected(session, tmp_path):
    script = tmp_path / "remote.py"
    script.write_text(
        "from remote_pdb import RemotePdb\n"
        "x = 7\n"
        "RemotePdb('127.0.0.1', 0).set_trace()\n"
        "y = x + 1\n"
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True
    )
    try:
        assert proc.stderr is not None
        banner = proc.stderr.readline()
        port = int(re.search(r":(\d+)", banner).group(1))  # type: ignore[union-attr]
        out = session.connect_remote_debug("127.0.0.1", port, timeout=5)
        assert "Connected" in out
        session.get_output(timeout=5)
        assert session.send_pdb_command("p x").strip().splitlines()[1] == "7"
        out = session.send_pdb_command("c")
        assert "session has ended" in out
        assert session.pdb_running is False
    finally:
        proc.kill()
        proc.wait()


class _StdioClient:
    """Minimal raw JSON-RPC client for the server's stdio transport."""

    def __init__(self):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", "from mcp_pdb import main; main()"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        self.responses: dict[int, dict] = {}
        self.non_json: list[str] = []
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()
        self.request(0, "initialize", protocolVersion="2025-06-18", capabilities={},
                     clientInfo={"name": "test", "version": "0"})  # fmt: skip
        self.wait(0)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def _read(self):
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                self.non_json.append(line)
                continue
            if "id" in msg:
                self.responses[msg["id"]] = msg

    def send(self, msg):
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()

    def request(self, id_, method, **params):
        self.send({"jsonrpc": "2.0", "id": id_, "method": method, "params": params})

    def call(self, id_, name, **arguments):
        self.request(id_, "tools/call", name=name, arguments=arguments)

    def wait(self, *ids, timeout=30.0):
        deadline = time.monotonic() + timeout
        while not all(i in self.responses for i in ids):
            assert time.monotonic() < deadline, f"no response for {ids}"
            time.sleep(0.01)
        return [self.responses[i] for i in ids]

    def text(self, id_):
        return self.wait(id_)[0]["result"]["content"][0]["text"]

    def close(self):
        self.proc.kill()
        self.proc.wait()


@pytest.fixture
def stdio_client():
    client = _StdioClient()
    yield client
    client.close()


def test_mcp_stdio_stream_is_pure_json_and_calls_are_serialised(stdio_client, project):
    """Pipelined calls (mcp>=2 runs sync tools in threads) must not interleave."""
    c = stdio_client
    c.call(1, "start_debug", file_path=str(project / "app.py"), caveman_mode=True)
    c.call(2, "send_pdb_command", command="p 'first'")
    c.call(3, "send_pdb_command", command="n")
    c.call(4, "end_debug")
    c.wait(1, 2, 3, 4)
    assert c.text(2).splitlines()[-1] == "'first'"
    assert "app.py(2)" in c.text(3)
    assert c.non_json == []


def test_mcp_tool_listing_and_plain_text_results(stdio_client):
    c = stdio_client
    init = c.wait(0)[0]["result"]
    assert init["serverInfo"]["name"] == "mcp-pdb"
    assert "start_debug" in init["instructions"]

    c.request(1, "tools/list")
    tools = {t["name"]: t for t in c.wait(1)[0]["result"]["tools"]}
    assert {"start_debug", "get_output", "interrupt", "end_debug"} <= set(tools)
    assert all("outputSchema" not in t for t in tools.values())
    assert tools["list_breakpoints"]["annotations"]["readOnlyHint"] is True
    assert tools["end_debug"]["annotations"]["destructiveHint"] is True

    c.call(2, "get_debug_status")
    result = c.wait(2)[0]["result"]
    assert "structuredContent" not in result, "text must not be sent twice"


@posix_only
def test_mcp_interrupt_reaches_an_in_flight_continue(stdio_client, tmp_path):
    """interrupt skips the session lock, so it works while `c` is blocking."""
    (tmp_path / "requirements.txt").touch()
    (tmp_path / "spin.py").write_text("import time\nwhile True:\n    time.sleep(0.05)\n")
    c = stdio_client
    c.call(1, "start_debug", file_path=str(tmp_path / "spin.py"), caveman_mode=True)
    c.wait(1)
    c.call(2, "send_pdb_command", command="c", timeout=20)
    time.sleep(0.5)
    started = time.monotonic()
    c.call(3, "interrupt")
    assert "in-flight command" in c.text(3)
    assert "Program interrupted" in c.text(2)
    assert time.monotonic() - started < 5, "continue should end at the interrupt, not its timeout"
    c.call(4, "end_debug")
    c.wait(4)
