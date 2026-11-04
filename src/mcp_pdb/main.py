"""MCP server that drives the Python debugger (pdb) for an LLM agent.

Two transports are supported:

* **local** - ``start_debug`` launches ``python -m pdb`` / ``pytest --pdb`` as a
  subprocess and talks to it over stdin/stdout.
* **remote** - ``connect_remote_debug`` attaches to a pdb listening on a TCP
  socket (``rpdb``, ``remote_pdb.RemotePdb`` ...).

IMPORTANT: this server speaks JSON-RPC over *stdout*.  Nothing in this module
may write to stdout - all diagnostics go through ``log`` (stderr).
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import queue
import re
import select
import shlex
import shutil
import signal
import socket as socket_module
import subprocess
import sys
import threading
import time
import traceback
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger("mcp_pdb")

mcp = FastMCP("mcp-pdb")

# --- Tunables ---------------------------------------------------------------

# Every wait returns as soon as pdb prints its prompt, so these are ceilings,
# not delays.
DEFAULT_TIMEOUT = 10.0  # ordinary commands (p, l, b, ...)
LONG_RUNNING_TIMEOUT = 30.0  # commands that run user code (c, r, unt, ...)
STARTUP_TIMEOUT = 60.0  # first prompt (uv may need to sync an env first)
MAX_OUTPUT_CHARS = 20_000  # per tool result; middle is elided beyond this

# Matches "(Pdb) " and nested-debugger prompts such as "((Pdb)) ".
_PROMPT_RE = re.compile(r"\(+Pdb\)+\s*$")
_PROMPT_PREFIX_RE = re.compile(r"^\(+Pdb\)+ ")
_BP_SET_RE = re.compile(r"^(?:\(+Pdb\)+ )?Breakpoint (\d+) at (.+):(\d+)\s*$", re.M)
_BP_DEL_RE = re.compile(
    r"^(?:\(+Pdb\)+ )?Deleted breakpoint (\d+) at (.+):(\d+)\s*$", re.M
)

# Commands that resume user code and may take a while to return a prompt.
LONG_RUNNING_COMMANDS = frozenset(
    {"c", "cont", "continue", "r", "return", "unt", "until", "run", "restart"}
)
# Commands after which verbose mode appends the source window.
NAV_COMMANDS = frozenset(
    {
        "n", "next", "s", "step", "c", "cont", "continue", "r", "return",
        "unt", "until", "j", "jump",
    }
)  # fmt: skip

NO_SESSION_MSG = (
    "No active debugging session. Use start_debug or connect_remote_debug first."
)

# With breakpoints set, pdb's tracer stays active during `continue`, so the
# set_trace() inside its own SIGINT handler gets traced and the interrupt
# stops in debugger internals instead of user code (CPython 3.10-3.13).
# Marking those modules as skipped makes the stop land in the user's frame
# (and keeps `s` from stepping into them).  Sent once after startup; the
# live Pdb instance is reached through its trace function.
_SKIP_INTERNALS_CMD = (
    "!(lambda d: setattr(d, 'skip', set(d.skip or ()) | "
    "{'bdb', 'pdb', 'linecache', 'contextlib'}))(__import__('sys').gettrace().__self__)"
)

# --- Global session state ---------------------------------------------------
# Tool calls are serialised by FastMCP (sync tools run on the event-loop
# thread), so plain module globals are safe here.

pdb_process: subprocess.Popen[bytes] | None = None
pdb_output_queue: queue.Queue[str] = queue.Queue()
pdb_running = False
at_prompt = False  # True when the last read ended on a (Pdb) prompt
current_file: str | None = None  # absolute path of the file being debugged
current_project_root: str | None = None
current_args = ""
current_use_pytest = False
output_thread: threading.Thread | None = None
caveman_mode = False  # compact output when True
# Set by the reader thread when its stream hits EOF.  Replaced per session so
# a stale reader can never flag a newer session as closed.
stream_eof = threading.Event()

# Breakpoints tracked across restarts:
# {abs_file_path: {line: {"bp_number": str | None, "condition": str | None,
#                         "project_root": str | None}}}
breakpoints: dict[str, dict[int, dict[str, Any]]] = {}

# Remote PDB session state
remote_socket: socket_module.socket | None = None
remote_mode = False
remote_host = ""
remote_port = 0


# --- Output plumbing --------------------------------------------------------


def _decode(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").rstrip("\r")


def _is_prompt(line: str) -> bool:
    return bool(_PROMPT_RE.search(line))


def _pump_stream(
    recv: Callable[[], bytes],
    more_pending: Callable[[], bool] | None,
    output_queue: queue.Queue[str],
    eof_event: threading.Event | None,
    label: str,
) -> None:
    """Read chunks from ``recv`` and push decoded lines onto ``output_queue``.

    pdb's ``(Pdb) `` prompt has no trailing newline, so a line-based reader
    would sit on it until the *next* command produced output.  Instead,
    after splitting off complete lines, any residue is emitted immediately
    when it looks like a prompt, or when ``more_pending`` reports that no
    further bytes are arriving (e.g. an ``input()`` prompt from user code).
    """
    buf = b""
    try:
        while True:
            try:
                chunk = recv()
            except (OSError, ValueError) as e:
                log.debug("%s reader: read error: %s", label, e)
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                output_queue.put(_decode(line))
            if buf:
                text = _decode(buf)
                if _is_prompt(text) or (more_pending is not None and not more_pending()):
                    output_queue.put(text)
                    buf = b""
    except Exception:
        log.exception("%s reader: unexpected error", label)
    finally:
        if buf:
            output_queue.put(_decode(buf))
        if eof_event is not None:
            eof_event.set()
        log.debug("%s reader thread finished.", label)


def read_pdb_output(
    process: subprocess.Popen[bytes],
    output_queue: queue.Queue[str],
    eof_event: threading.Event | None = None,
) -> None:
    """Reader thread for a local pdb subprocess."""
    stdout = process.stdout
    if stdout is None:
        if eof_event is not None:
            eof_event.set()
        return
    fd = stdout.fileno()

    def pipe_has_data() -> bool:
        return bool(select.select([fd], [], [], 0.05)[0])

    # select() does not support pipes on Windows; rely on prompt detection.
    more_pending = None if sys.platform == "win32" else pipe_has_data
    try:
        _pump_stream(
            lambda: os.read(fd, 4096), more_pending, output_queue, eof_event, "local"
        )
    finally:
        with contextlib.suppress(Exception):
            stdout.close()


def read_socket_output(
    sock: socket_module.socket,
    output_queue: queue.Queue[str],
    eof_event: threading.Event | None = None,
) -> None:
    """Reader thread for a remote pdb socket."""

    def more_pending() -> bool:
        return bool(select.select([sock], [], [], 0.05)[0])

    _pump_stream(lambda: sock.recv(4096), more_pending, output_queue, eof_event, "remote")


def _drain_queue() -> list[str]:
    lines: list[str] = []
    while True:
        try:
            lines.append(pdb_output_queue.get_nowait())
        except queue.Empty:
            return lines


def _collect_output(timeout: float) -> tuple[list[str], bool]:
    """Collect queued lines until a prompt, EOF, or ``timeout`` seconds.

    Returns ``(lines, saw_prompt)``.
    """
    lines: list[str] = []
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return lines, False
        try:
            line = pdb_output_queue.get(timeout=min(remaining, 0.1))
        except queue.Empty:
            # The reader puts its last line before setting the event, so an
            # empty queue after the event means everything has been read.
            if stream_eof.is_set() and pdb_output_queue.empty():
                return lines, False
            continue
        lines.append(line)
        if _is_prompt(line):
            return lines, True


def _format_output(lines: list[str]) -> str:
    """Join output lines; in caveman mode drop pdb prompts."""
    if not caveman_mode:
        return "\n".join(lines)
    cleaned = []
    for line in lines:
        if _PROMPT_RE.fullmatch(line.strip()):
            continue
        cleaned.append(_PROMPT_PREFIX_RE.sub("", line, count=1))
    return "\n".join(cleaned)


def get_pdb_output(timeout: float = 0.5) -> str:
    """Return output accumulated until the next prompt (or ``timeout``)."""
    global at_prompt
    lines, saw_prompt = _collect_output(timeout)
    if lines:
        at_prompt = saw_prompt
    return _format_output(lines)


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = limit * 2 // 3
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n[... {omitted} characters omitted ...]\n\n{text[-tail:]}"


def _command_word(command: str) -> str:
    parts = command.strip().split(maxsplit=1)
    return parts[0].lower() if parts else ""


# --- Session helpers --------------------------------------------------------


def _set_caveman(value: bool) -> None:
    global caveman_mode
    caveman_mode = bool(value)


def _is_local_process_alive() -> bool:
    return pdb_process is not None and pdb_process.poll() is None


def _is_remote_connected() -> bool:
    """True while the remote socket is open and its reader has not seen EOF."""
    return remote_socket is not None and not stream_eof.is_set()


def _session_alive() -> bool:
    return _is_remote_connected() if remote_mode else _is_local_process_alive()


def _clear_remote_state() -> None:
    """Reset every remote_* global to the idle default.

    Invariant: if ``remote_socket is None`` then ``remote_mode is False``
    and host/port are empty - there is no valid intermediate state.
    """
    global remote_socket, remote_mode, remote_host, remote_port
    remote_socket = None
    remote_mode = False
    remote_host = ""
    remote_port = 0


def _close_socket(sock: socket_module.socket) -> None:
    # shutdown() before close() so a reader blocked in recv()/select()
    # unblocks immediately on every POSIX implementation.
    with contextlib.suppress(OSError):
        sock.shutdown(socket_module.SHUT_RDWR)
    with contextlib.suppress(OSError):
        sock.close()


def _signal_group(proc: subprocess.Popen[bytes], sig: int) -> bool:
    """Send ``sig`` to the debuggee's process group. Returns False if gone."""
    try:
        if sys.platform == "win32":
            if sig == signal.SIGINT:
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            elif sig == signal.SIGTERM:
                proc.terminate()
            else:
                proc.kill()
        else:
            os.killpg(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True


def _relpath_for_pdb(abs_path: str) -> str:
    """Path to hand to pdb: relative to the project root when inside it."""
    if current_project_root:
        try:
            rel = os.path.relpath(abs_path, current_project_root)
        except ValueError:  # different drive on Windows
            return abs_path
        if not rel.startswith(".."):
            return rel
    return abs_path


def _sync_breakpoints(output: str) -> None:
    """Mirror pdb's own breakpoint confirmations into ``breakpoints``.

    This catches breakpoints set or cleared through raw ``send_pdb_command``
    calls too, so restarts restore everything pdb knew about.  Remote
    sessions are skipped: their paths live on another machine/process.
    """
    if remote_mode:
        return
    for num, path, line in _BP_SET_RE.findall(output):
        entry = breakpoints.setdefault(os.path.abspath(path), {}).setdefault(
            int(line), {"condition": None}
        )
        entry["bp_number"] = num
        entry["project_root"] = current_project_root
    for num, path, line in _BP_DEL_RE.findall(output):
        file_bps = breakpoints.get(os.path.abspath(path), {})
        entry = file_bps.get(int(line))
        if entry is not None and entry.get("bp_number") in (num, None):
            del file_bps[int(line)]
            if not file_bps:
                breakpoints.pop(os.path.abspath(path), None)


def _format_tracked_breakpoints() -> list[str]:
    items = []
    for abs_path in sorted(breakpoints):
        for line_num in sorted(breakpoints[abs_path]):
            data = breakpoints[abs_path][line_num]
            item = f"{_relpath_for_pdb(abs_path)}:{line_num}"
            if data.get("bp_number"):
                item += f" (BP #{data['bp_number']})"
            if data.get("condition"):
                item += f" if {data['condition']}"
            items.append(item)
    return items


def _session_ended_notice() -> str:
    return "\n\n*** The debugging session has ended. ***"


def send_to_pdb(command: str, timeout: float | None = None) -> str:
    """Send a command to pdb (local or remote) and return its response.

    Waits until pdb prints its next prompt, the stream closes, or
    ``timeout`` seconds pass.  Output that arrived *between* commands (e.g.
    the tail of a long ``continue``) is returned first rather than dropped.
    """
    global pdb_running, at_prompt

    if not _session_alive():
        if pdb_running:
            pdb_running = False
            leftover = _format_output(_drain_queue())
            return f"The debugging session has ended.\nFinal output:\n{leftover}"
        return NO_SESSION_MSG

    pending = _drain_queue()
    if timeout is None:
        timeout = (
            LONG_RUNNING_TIMEOUT
            if _command_word(command) in LONG_RUNNING_COMMANDS
            else DEFAULT_TIMEOUT
        )

    rsock = remote_socket if remote_mode else None
    lproc = None if remote_mode else pdb_process
    try:
        data = (command + "\n").encode("utf-8")
        if rsock is not None:
            rsock.sendall(data)
        else:
            assert lproc is not None  # guaranteed by _session_alive()
            if lproc.stdin is None:
                return "Error: PDB process stdin is not available."
            lproc.stdin.write(data)
            lproc.stdin.flush()
        at_prompt = False
        lines, saw_prompt = _collect_output(timeout)
    except OSError as e:
        log.warning("Error communicating with PDB: %s", e)
        pdb_running = False
        at_prompt = False
        if rsock is not None:
            _close_socket(rsock)
            _clear_remote_state()
        elif lproc is not None:
            _signal_group(lproc, signal.SIGTERM)
        leftover = _format_output(pending + _drain_queue())
        return (
            f"Error communicating with PDB: {e}\nFinal output:\n{leftover}"
            + _session_ended_notice()
        )

    at_prompt = saw_prompt
    raw = "\n".join(pending + lines)
    _sync_breakpoints(raw)

    output = _format_output(lines)
    pending_text = _format_output(pending).strip()
    if pending_text:
        output = (
            "[Output received since the previous command]\n"
            f"{pending_text}\n[Response]\n{output}"
        )

    if not _session_alive():
        # Give the reader a moment to deliver the process's last words.
        if not remote_mode and lproc is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                lproc.wait(timeout=1.0)
        more, _ = _collect_output(0.5)
        if more:
            output += "\n" + _format_output(more)
        pdb_running = False
        return _truncate(output) + _session_ended_notice()

    if not saw_prompt:
        output += (
            f"\n\n[No (Pdb) prompt after {timeout:g}s - the program is still "
            "running. Call get_output to wait for more output, or interrupt "
            "to pause it.]"
        )
    return _truncate(output)


# --- Environment discovery --------------------------------------------------


def find_project_root(start_path: str) -> str:
    """Find the nearest ancestor containing a project marker."""
    current_dir = os.path.abspath(start_path)
    root_indicators = [
        "pyproject.toml", ".git", "setup.py", "requirements.txt", "Pipfile",
        "poetry.lock",
    ]  # fmt: skip
    while current_dir and current_dir != os.path.dirname(current_dir):
        for indicator in root_indicators:
            if os.path.exists(os.path.join(current_dir, indicator)):
                log.info("Found project root indicator %r at %s", indicator, current_dir)
                return current_dir
        current_dir = os.path.dirname(current_dir)

    fallback_dir = os.path.abspath(start_path)
    log.info("No project root indicators found; using %s", fallback_dir)
    return fallback_dir


def _venv_python(venv_path: str) -> tuple[str, str]:
    if sys.platform == "win32":
        bin_dir = os.path.join(venv_path, "Scripts")
        return os.path.join(bin_dir, "python.exe"), bin_dir
    bin_dir = os.path.join(venv_path, "bin")
    return os.path.join(bin_dir, "python"), bin_dir


def find_venv_details(project_root: str) -> tuple[str | None, str | None]:
    """Return ``(python_exe, bin_dir)`` for the project's virtualenv, if any.

    Venv directories in the project (and its parent) are checked before
    VIRTUAL_ENV / CONDA_PREFIX, because when mcp-pdb itself runs under uv
    those variables describe mcp-pdb's environment, not the debuggee's.
    Returns ``(None, None)`` when nothing is found; the caller then falls
    back to the system interpreter.
    """
    common_venv_names = [".venv", "venv", "env", ".env", "virtualenv", ".virtualenv"]
    locations = [project_root]
    parent_dir = os.path.dirname(project_root)
    if parent_dir != project_root:
        locations.append(parent_dir)

    for location in locations:
        for name in common_venv_names:
            venv_path = os.path.join(location, name)
            if os.path.isdir(venv_path):
                python_exe, bin_dir = _venv_python(venv_path)
                if os.path.exists(python_exe):
                    log.info("Found virtual environment: %s", venv_path)
                    return python_exe, bin_dir

    venv_env = os.environ.get("VIRTUAL_ENV")
    if venv_env and os.path.isdir(venv_env):
        python_exe, bin_dir = _venv_python(venv_env)
        if os.path.exists(python_exe):
            log.info("Using VIRTUAL_ENV: %s", venv_env)
            return python_exe, bin_dir

    conda_prefix = os.environ.get("CONDA_PREFIX")
    if conda_prefix:
        if sys.platform == "win32":
            python_exe, bin_dir = os.path.join(conda_prefix, "python.exe"), conda_prefix
        else:
            bin_dir = os.path.join(conda_prefix, "bin")
            python_exe = os.path.join(bin_dir, "python")
        if os.path.exists(python_exe):
            log.info("Using CONDA_PREFIX: %s", conda_prefix)
            return python_exe, bin_dir

    log.info("No virtual environment found for %s", project_root)
    return None, None


def parse_arguments(args_str: str) -> list[str]:
    """Split a script/pytest argument string into argv items.

    The subprocess is launched with an argv list (no shell), so shell
    metacharacters such as ``|`` or ``>`` are passed through literally and
    are harmless; only quoting needs to be valid.
    """
    try:
        return shlex.split(args_str)
    except ValueError as e:
        raise ValueError(f"Error parsing arguments: {e}") from e


def _resolve_file(file_path: str) -> str | None:
    cwd = os.getcwd()
    candidates = [file_path, os.path.join(cwd, file_path)]
    candidates += [os.path.join(cwd, d, file_path) for d in ("src", "tests", "lib")]
    for path in candidates:
        if os.path.exists(path):
            return os.path.abspath(path)
    return None


def _build_command(
    project_root: str, target: str, use_pytest: bool, parsed_args: list[str]
) -> tuple[list[str], dict[str, str]] | str:
    """Return ``(argv, env)`` for the debuggee, or an error message."""
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"  # see program output as it happens
    env["PYTHONIOENCODING"] = "utf-8"
    env.pop("PYTHONHOME", None)
    pytest_flags = ["--pdb", "-s", "--pdbcls=pdb:Pdb"]

    if shutil.which("uv") and os.path.exists(os.path.join(project_root, "pyproject.toml")):
        log.info("Using uv run in %s", project_root)
        env.pop("VIRTUAL_ENV", None)  # would point at mcp-pdb's own env
        tool = ["pytest", *pytest_flags] if use_pytest else ["python", "-m", "pdb"]
        return ["uv", "run", "--", *tool, target, *parsed_args], env

    venv_python, venv_bin = find_venv_details(project_root)
    if venv_python and venv_bin:
        log.info("Using venv Python: %s", venv_python)
        env["VIRTUAL_ENV"] = os.path.dirname(venv_bin)
        env["PATH"] = f"{venv_bin}{os.pathsep}{env.get('PATH', '')}"
        env["PYTHONPATH"] = f"{project_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
        python_exe = venv_python
    else:
        log.warning("No uv project or venv detected; using system Python.")
        found = shutil.which("python3") or shutil.which("python") or sys.executable
        if not found:
            return "Error: Could not find a Python interpreter in PATH."
        python_exe = found

    if not use_pytest:
        return [python_exe, "-m", "pdb", target, *parsed_args], env
    probe = subprocess.run(
        [python_exe, "-m", "pytest", "--version"],
        capture_output=True,
        cwd=project_root,
        env=env,
        check=False,
    )
    if probe.returncode != 0:
        return f"Error: pytest is not installed for {python_exe}. Cannot run with use_pytest=True."
    return [python_exe, "-m", "pytest", *pytest_flags, target, *parsed_args], env


def _restore_breakpoints() -> str:
    """Re-send tracked breakpoints belonging to the current project."""
    todo = [
        (abs_path, line, data)
        for abs_path, lines in breakpoints.items()
        for line, data in lines.items()
        if data.get("project_root") in (None, current_project_root)
    ]
    if not todo:
        return ""
    out = ["--- Restoring Breakpoints ---"]
    for abs_path, line, data in sorted(todo, key=lambda t: (t[0], t[1])):
        target = f"{_relpath_for_pdb(abs_path)}:{line}"
        if not os.path.exists(abs_path):
            out.append(f"{target}: skipped (file no longer exists)")
            continue
        data["bp_number"] = None  # pdb numbers afresh in a new process
        cmd = f"b {target}" + (f", {data['condition']}" if data.get("condition") else "")
        response = _format_output(
            [ln for ln in send_to_pdb(cmd).splitlines() if not _is_prompt(ln)]
        ).strip()
        out.append(f"{target}: {response or '[no response]'}")
    return "\n".join(out)


# --- MCP tools --------------------------------------------------------------


@mcp.tool()
def start_debug(
    file_path: str, use_pytest: bool = False, args: str = "", caveman_mode: bool = False
) -> str:
    """Start a local debugging session on a Python file within its project.

    The runner is picked automatically: ``uv run`` for projects with a
    pyproject.toml (when uv is installed), else the project's virtualenv,
    else the system Python.  Breakpoints tracked for this project from
    earlier sessions are restored.

    Args:
        file_path: Path to the Python file or test module to debug.
        use_pytest: If True, run under ``pytest --pdb`` (stops at the first failure).
        args: Extra arguments for the script or pytest, shell-quoted
              (e.g. ``-k "test_a or test_b" -x``).
        caveman_mode: Compact output (default False).  When True: no source
              window after navigation commands, no dir() listing in
              examine_variable unless include_attrs=True, and bare (Pdb)
              prompts stripped.  Persists across restart_debug.
    """
    global pdb_process, pdb_running, at_prompt, current_file, current_project_root
    global output_thread, current_args, current_use_pytest, stream_eof

    if pdb_running and _session_alive():
        where = f"{remote_host}:{remote_port}" if remote_mode else current_file
        return f"Debugging session already running for {where}. Use restart_debug or end_debug first."
    if remote_mode:
        end_debug()
    pdb_running = False
    _set_caveman(caveman_mode)

    abs_file_path = _resolve_file(file_path)
    if not abs_file_path:
        return f"Error: File not found at '{file_path}' (checked as given, and under CWD, src/, tests/, lib/)."

    project_root = find_project_root(os.path.dirname(abs_file_path))
    try:
        parsed_args = parse_arguments(args)
    except ValueError as e:
        return f"Error in arguments: {e}"

    current_project_root = project_root
    current_file = abs_file_path
    current_args = args
    current_use_pytest = use_pytest
    target = _relpath_for_pdb(abs_file_path)

    built = _build_command(project_root, target, use_pytest, parsed_args)
    if isinstance(built, str):
        return built
    cmd, env = built
    cmd_str = " ".join(map(shlex.quote, cmd))
    log.info("Executing: %s (cwd=%s)", cmd_str, project_root)

    _drain_queue()
    stream_eof = threading.Event()
    try:
        # Own process group: `uv run` spawns python as a grandchild, and
        # signalling the whole group is the only reliable way to interrupt
        # or kill the real debuggee.
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=project_root,
            env=env,
            bufsize=0,
            start_new_session=sys.platform != "win32",
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except FileNotFoundError as e:
        return f"Error starting debugging session: command not found ({e.filename}). Command: {cmd_str}"
    except Exception as e:
        return f"Error starting debugging session: {e}\n{traceback.format_exc()}"
    pdb_process = proc

    output_thread = threading.Thread(
        target=read_pdb_output, args=(proc, pdb_output_queue, stream_eof), daemon=True
    )
    output_thread.start()
    pdb_running = True

    lines, saw_prompt = _collect_output(STARTUP_TIMEOUT)
    at_prompt = saw_prompt
    initial_output = _format_output(lines)

    if not saw_prompt:
        if not _is_local_process_alive():
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=1.0)
            rest = _format_output(_drain_queue())
            pdb_running = False
            hint = ""
            if use_pytest:
                hint = "\n(Under pytest --pdb, pdb only opens on a failure or error; if every test passed there is nothing to debug.)"
            return (
                f"Error: PDB process exited before reaching a prompt (code {proc.returncode}).\n"
                f"Command: {cmd_str}\nWorking dir: {project_root}\n"
                f"Output:\n---\n{_truncate(initial_output + rest)}\n---{hint}"
            )
        initial_output += (
            f"\n\n[Warning: no (Pdb) prompt within {STARTUP_TIMEOUT:g}s; the "
            "program is still running. Use get_output to wait for it.]"
        )

    restored = ""
    if saw_prompt:
        if not use_pytest:  # pytest turns SIGINT into its own pdb entry
            send_to_pdb(_SKIP_INTERNALS_CMD)
        restored = _restore_breakpoints()
    return _truncate(
        f"Debugging session started for {target} (in {project_root})\n\n"
        f"{initial_output}\n{restored}"
    ).rstrip()


@mcp.tool()
def connect_remote_debug(
    host: str = "localhost", port: int = 4444, timeout: int = 30, caveman_mode: bool = False
) -> str:
    """Connect to a remote PDB session listening on a TCP socket.

    Success means "TCP connection established": the tool does not wait for
    a ``(Pdb)`` prompt, because with ``rpdb script.py`` the script only
    starts once the client connects.  Commands sent before the remote side
    reaches a prompt queue on the socket.

    Works with ``rpdb script.py`` (dual terminal + agent session) and
    ``remote_pdb.RemotePdb().set_trace()``.

    Args:
        host: Hostname or IP of the remote pdb (default "localhost").
        port: TCP port the remote pdb listens on (default 4444).
        timeout: Seconds to keep retrying a refused connection (default 30;
                 0 for a single attempt).
        caveman_mode: Compact output mode; see start_debug.
    """
    global pdb_running, remote_socket, remote_mode, remote_host, remote_port
    global output_thread, stream_eof, at_prompt

    if pdb_running and _session_alive():
        if remote_mode:
            return f"Already connected to remote PDB at {remote_host}:{remote_port}. Use end_debug first."
        return f"Local debugging session already running for {current_file}. Use end_debug first."
    if pdb_process is not None:
        end_debug()
    pdb_running = False
    _set_caveman(caveman_mode)
    _drain_queue()

    deadline = time.monotonic() + max(timeout, 0)
    last_error: OSError | None = None
    sock = None
    log.info("Connecting to remote PDB at %s:%s (timeout=%ss)", host, port, timeout)
    while True:
        try:
            sock = socket_module.create_connection((host, port), timeout=5.0)
            sock.settimeout(None)
            break
        except OSError as e:
            last_error = e
            remaining = deadline - time.monotonic()
            if timeout == 0 or remaining <= 0:
                break
            time.sleep(min(1.0, remaining))

    if sock is None:
        return f"Failed to connect to {host}:{port} within {timeout}s. Last error: {last_error}"

    stream_eof = threading.Event()
    t = threading.Thread(
        target=read_socket_output, args=(sock, pdb_output_queue, stream_eof), daemon=True
    )
    try:
        t.start()
        # Best effort: a RemotePdb.set_trace() target is already at a prompt.
        lines, saw_prompt = _collect_output(0.3)
    except Exception as e:
        _close_socket(sock)
        return f"Error setting up remote connection: {e}\n{traceback.format_exc()}"

    # Commit state; pdb_running last so no tool sees a half-built session.
    remote_socket = sock
    remote_mode = True
    remote_host = host
    remote_port = port
    output_thread = t
    at_prompt = saw_prompt
    pdb_running = True

    initial_output = _format_output(lines)
    if initial_output.strip():
        return f"Connected to remote PDB at {host}:{port}\n\nInitial output:\n{initial_output}"
    return (
        f"Connected to remote PDB at {host}:{port}\n\n"
        "(No prompt yet - the remote process has not reached a breakpoint. "
        "Commands sent via send_pdb_command will queue on the socket and be "
        "processed when PDB starts reading; get_output waits for the prompt.)"
    )


@mcp.tool()
def send_pdb_command(command: str, timeout: float | None = None) -> str:
    """Send a command to the running PDB instance.

    Returns as soon as pdb shows its next prompt.  If the program is still
    running when the timeout expires, the partial output is returned with a
    note; call get_output to keep waiting, or interrupt to pause it.

    Examples:
        n (next), s (step into), c (continue), r (return), unt 42 (run to line 42)
        p expr, pp expr, w (stack), u / d (move up/down the stack)
        b 42, b file.py:42, b func, b 42, x > 3 (conditional), cl 3
        l / ll (list source), a (function args), q (quit)

    Breakpoints set or cleared here are tracked and restored on restart.

    Args:
        command: The PDB command string.
        timeout: Max seconds to wait for the prompt.  Default 30 for commands
                 that run code (c, r, unt, ...), 10 otherwise.
    """
    global pdb_running
    if not pdb_running:
        return NO_SESSION_MSG
    if "\n" in command.strip():
        return "Error: send one command per call (no newlines). Use ';;' to chain pdb commands."

    response = send_to_pdb(command, timeout)
    if not pdb_running:
        return f"Command output:\n{response}"

    if not caveman_mode and at_prompt and _command_word(command) in NAV_COMMANDS:
        context = send_to_pdb("l .")
        if pdb_running:
            response += f"\n\n-- Current location --\n{context}"
        else:
            response += f"\n\n-- Session ended after navigation --\n{context}"

    return f"Command output:\n{response}"


@mcp.tool()
def get_output(timeout: float = 10.0) -> str:
    """Wait for more output from the debuggee without sending a command.

    Use after a command returned "still running", or for a remote session
    that has not reached a breakpoint yet.  Returns when pdb prints its
    prompt, the program exits, or ``timeout`` seconds pass.
    """
    global pdb_running, at_prompt
    if not pdb_running:
        return NO_SESSION_MSG
    lines, saw_prompt = _collect_output(timeout)
    _sync_breakpoints("\n".join(lines))
    if lines:
        at_prompt = saw_prompt
    output = _format_output(lines)
    if not _session_alive():
        output += "\n" + _format_output(_drain_queue())
        pdb_running = False
        return _truncate(output) + _session_ended_notice()
    if at_prompt:
        status = "[At (Pdb) prompt]"
    else:
        status = f"[Still running after {timeout:g}s - no prompt yet]"
    return _truncate(f"{output}\n{status}" if output.strip() else status)


@mcp.tool()
def interrupt() -> str:
    """Pause a running program (like pressing Ctrl+C) and return to the prompt.

    Local sessions only.  Under ``python -m pdb`` this stops at the line
    currently executing; under pytest it raises KeyboardInterrupt in the test.
    """
    if not pdb_running:
        return NO_SESSION_MSG
    if remote_mode:
        return "interrupt is not supported for remote sessions; press Ctrl+C in the remote process's terminal."
    if at_prompt:
        return "Already at the (Pdb) prompt; nothing to interrupt."
    assert pdb_process is not None
    if not _signal_group(pdb_process, signal.SIGINT):
        return "Could not signal the debuggee (it may have exited)."
    return "Sent interrupt.\n" + get_output(timeout=5.0)


@mcp.tool()
def set_breakpoint(file_path: str, line_number: int, condition: str = "") -> str:
    """Set a breakpoint at a line, optionally conditional. Tracked across restarts.

    Args:
        file_path: Path to the file (relative to the project root, or absolute).
        line_number: Line number for the breakpoint.
        condition: Optional Python expression; pdb stops only when it is true
                   (e.g. ``i == 99`` or ``user is None``).
    """
    if not pdb_running:
        return NO_SESSION_MSG
    if remote_mode:
        target = f"{file_path}:{line_number}"
    else:
        root = current_project_root or os.getcwd()
        abs_file_path = os.path.abspath(os.path.join(root, file_path))
        if not os.path.exists(abs_file_path):
            return f"Error: File not found at '{file_path}' (checked relative to {root} and as absolute)."
        target = f"{_relpath_for_pdb(abs_file_path)}:{line_number}"

    condition = condition.strip()
    response = send_to_pdb(f"b {target}" + (f", {condition}" if condition else ""))
    match = _BP_SET_RE.search(response)
    if not match:
        return f"Failed to set breakpoint. PDB response:\n{response}"
    if condition and not remote_mode:
        breakpoints[os.path.abspath(match.group(2))][int(match.group(3))]["condition"] = condition
    return f"Breakpoint #{match.group(1)} set{' (if ' + condition + ')' if condition else ''}:\n{response}"


@mcp.tool()
def clear_breakpoint(file_path: str, line_number: int) -> str:
    """Clear the breakpoint(s) at a specific line in a file.

    Args:
        file_path: Path to the file where the breakpoint exists.
        line_number: Line number of the breakpoint to clear.
    """
    if not pdb_running:
        return NO_SESSION_MSG
    abs_file_path = None
    if remote_mode:
        target = f"{file_path}:{line_number}"
    else:
        root = current_project_root or os.getcwd()
        abs_file_path = os.path.abspath(os.path.join(root, file_path))
        target = f"{_relpath_for_pdb(abs_file_path)}:{line_number}"

    response = send_to_pdb(f"cl {target}")
    if "Deleted breakpoint" in response:
        return f"Breakpoint cleared:\n{response}"
    if abs_file_path and "There is no breakpoint at" in response:
        # pdb has none there; drop any stale tracking so restarts agree.
        file_bps = breakpoints.get(abs_file_path, {})
        if file_bps.pop(line_number, None) is not None and not file_bps:
            breakpoints.pop(abs_file_path, None)
    return f"No breakpoint cleared. PDB response:\n{response}"


@mcp.tool()
def list_breakpoints() -> str:
    """List breakpoints known to PDB alongside those tracked for restarts."""
    tracked = _format_tracked_breakpoints()
    tracked_text = "\n".join(tracked) if tracked else "None"
    if not pdb_running:
        return f"No active PDB session.\n\n--- Tracked Breakpoints ---\n{tracked_text}"
    pdb_response = send_to_pdb("b")
    return (
        f"--- PDB Breakpoints ---\n{pdb_response}\n\n"
        f"--- Tracked Breakpoints (restored on restart) ---\n{tracked_text}"
    )


@mcp.tool()
def restart_debug() -> str:
    """Restart the session with the same file/args/pytest flag (or reconnect remote).

    Tracked breakpoints - in any file of the project - are restored, and
    caveman_mode is preserved.
    """
    keep_caveman = caveman_mode
    if remote_mode:
        host, port = remote_host, remote_port
        end_result = end_debug()
        reconnect = connect_remote_debug(host=host, port=port, caveman_mode=keep_caveman)
        return f"--- Remote Restart ---\nPrevious session: {end_result}\n\n{reconnect}"

    if not current_file:
        return "No debugging session was previously started to restart."
    file_to_debug, args_to_use, use_pytest = current_file, current_args, current_use_pytest
    end_result = end_debug() if pdb_running or _is_local_process_alive() else "Previous session had already ended."
    start_result = start_debug(
        file_path=file_to_debug, use_pytest=use_pytest, args=args_to_use, caveman_mode=keep_caveman
    )
    return f"--- Restart ---\nPrevious session: {end_result}\n\n{start_result}"


_EXAMINE_CODE = """\
import pprint
def _cut(s):
    return s if len(s) <= MAX else s[:MAX] + f'... [truncated; {len(s)} chars total]'
t = type(_v)
print('Type:', t.__qualname__ if t.__module__ == 'builtins' else t.__module__ + '.' + t.__qualname__)
try:
    print('Len:', len(_v))
except Exception:
    pass
try:
    r = repr(_v)
except Exception as e:
    r = None
    print('Value: <repr() failed: %r>' % (e,))
if r is not None:
    print('Value:', _cut(r))
    if PRETTY:
        p = pprint.pformat(_v, width=100)
        if p != r:
            print('Pretty:')
            print(_cut(p))
if ATTRS:
    names = dir(_v)
    public = [n for n in names if not n.startswith('__')]
    print('Attrs:', _cut(repr(public)), '(+%d dunder)' % (len(names) - len(public)))
"""


def _examine_command(expr: str, pretty: bool, attrs: bool, max_chars: int) -> str:
    """Build one pdb line that reports on ``expr`` without touching the frame.

    ``expr`` is evaluated once, in the current frame, and handed to helper
    code running in a throwaway namespace, so no names leak into the
    program being debugged.
    """
    code = (
        _EXAMINE_CODE.replace("MAX", str(max(int(max_chars), 100)))
        .replace("PRETTY", str(pretty))
        .replace("ATTRS", str(attrs))
    )
    return f"!__import__('builtins').exec({code!r}, {{'_v': ({expr})}})"


@mcp.tool()
def examine_variable(variable_name: str, include_attrs: bool = False, max_chars: int = 4000) -> str:
    """Examine a variable or expression: type, length, value and (optionally) attributes.

    One round-trip; the expression is evaluated once in the current frame.

    Args:
        variable_name: Variable or expression (e.g. ``my_var``, ``self.data[0]``).
        include_attrs: Also list dir() attributes.  Always on when
                       caveman_mode is False.
        max_chars: Truncate the repr / pretty-print beyond this many characters.
    """
    if not pdb_running:
        return NO_SESSION_MSG
    expr = variable_name.strip()
    if not expr or "\n" in expr:
        return "Error: variable_name must be a single-line expression."
    response = send_to_pdb(
        _examine_command(
            expr, pretty=not caveman_mode, attrs=include_attrs or not caveman_mode, max_chars=max_chars
        )
    )
    if caveman_mode:
        return response
    return f"--- Variable Examination: {expr} ---\n{response}"


@mcp.tool()
def get_debug_status() -> str:
    """Get the current status of the debugging session and tracked state."""
    global pdb_running
    if pdb_running and not _session_alive():
        pdb_running = False
        return "Debugging session has ended " + (
            "(remote connection closed)." if remote_mode else "(process terminated)."
        )
    if not pdb_running:
        return "No active debugging session."

    tracked = _format_tracked_breakpoints() or "None"
    if remote_mode:
        lines = [
            "--- Debug Session Status ---",
            "Mode: remote",
            f"Remote host: {remote_host}:{remote_port}",
        ]
    else:
        assert pdb_process is not None
        lines = [
            "--- Debug Session Status ---",
            "Mode: local",
            f"PID: {pdb_process.pid}",
            f"Project Root: {current_project_root}",
            f"Debugging File: {current_file}",
            f"Using Pytest: {current_use_pytest}",
            f"Arguments: {current_args!r}",
        ]
    lines += [f"Caveman mode: {caveman_mode}", f"Tracked Breakpoints: {tracked}", ""]

    if not at_prompt:
        # Sending a command now would be read by the program, not pdb.
        lines.append("-- Program is running (not at a (Pdb) prompt). Use get_output or interrupt. --")
    elif caveman_mode:
        lines += ["-- Stack (w) --", send_to_pdb("w")]
    else:
        lines += ["-- Current location --", send_to_pdb("l .")]
    lines.append("--- End Status ---")
    return "\n".join(lines)


@mcp.tool()
def end_debug() -> str:
    """End the current debugging session (local process or remote connection)."""
    global pdb_process, pdb_running, output_thread, at_prompt

    if not pdb_running and pdb_process is None and remote_socket is None:
        return "No active debugging session to end."

    result_message = "Debugging session ended."
    if remote_mode or remote_socket is not None:
        if remote_socket is not None:
            with contextlib.suppress(OSError):
                remote_socket.sendall(b"q\n")  # politely ask pdb to quit
                time.sleep(0.2)
            _close_socket(remote_socket)
        _clear_remote_state()
    elif pdb_process is not None:
        proc = pdb_process
        if proc.poll() is None:
            # 1. Ask pdb to quit and close stdin (EOF also makes pdb exit).
            with contextlib.suppress(OSError, ValueError):
                if proc.stdin is not None:
                    proc.stdin.write(b"q\n")
                    proc.stdin.flush()
                    proc.stdin.close()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                # 2. SIGTERM, then 3. SIGKILL the whole process group.
                _signal_group(proc, signal.SIGTERM)
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    _signal_group(proc, signal.SIGKILL)
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        result_message = f"Warning: PDB process {proc.pid} did not exit after SIGKILL."
        # Reap any grandchildren left behind (e.g. python under `uv run`).
        if sys.platform != "win32":
            _signal_group(proc, signal.SIGKILL)

    pdb_process = None
    pdb_running = False
    at_prompt = False
    _set_caveman(False)  # don't leak into a later session started without it

    if output_thread is not None and output_thread.is_alive():
        output_thread.join(timeout=1.0)
        if output_thread.is_alive():
            log.warning("Output reader thread did not finish cleanly.")
    output_thread = None
    _drain_queue()
    return result_message


# --- Cleanup on exit --------------------------------------------------------


def cleanup() -> None:
    """Make sure the debuggee / socket does not outlive the server."""
    if pdb_running or pdb_process is not None or remote_socket is not None:
        end_debug()


atexit.register(cleanup)


def main() -> None:
    """Run the MCP server over stdio."""
    logging.basicConfig(
        stream=sys.stderr,
        level=os.environ.get("MCP_PDB_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log.info("Starting mcp-pdb (python=%s, cwd=%s)", sys.executable, os.getcwd())
    mcp.run()


if __name__ == "__main__":
    main()
