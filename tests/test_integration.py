"""End-to-end tests that drive real pdb processes through the tool functions."""

import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
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


def test_mcp_stdio_stream_is_pure_json(project):
    """Every line the server writes to stdout must be a JSON-RPC message."""
    server = subprocess.Popen(
        [sys.executable, "-c", "from mcp_pdb import main; main()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert server.stdin is not None
    assert server.stdout is not None

    def send(msg):
        server.stdin.write(json.dumps(msg) + "\n")  # type: ignore[union-attr]
        server.stdin.flush()  # type: ignore[union-attr]

    def call(id_, name, **arguments):
        send({"jsonrpc": "2.0", "id": id_, "method": "tools/call",
              "params": {"name": name, "arguments": arguments}})  # fmt: skip

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "test", "version": "0"}}})  # fmt: skip
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        call(2, "start_debug", file_path=str(project / "app.py"))
        call(3, "send_pdb_command", command="n")
        call(4, "end_debug")

        responses = {}
        deadline = time.monotonic() + 30
        while len(responses) < 4 and time.monotonic() < deadline:
            line = server.stdout.readline()
            if not line:
                break
            msg = json.loads(line)  # raises if anything non-JSON leaks
            if "id" in msg:
                responses[msg["id"]] = msg
        assert set(responses) == {1, 2, 3, 4}
        assert "app.py(2)" in responses[3]["result"]["content"][0]["text"]
    finally:
        server.kill()
        server.wait()
