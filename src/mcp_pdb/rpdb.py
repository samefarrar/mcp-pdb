"""
rpdb.py — remote-pdb wrapper, Python 3.10-compatible.

Usage modes:

  1. Dual session — user keeps a local terminal AND mcp-pdb connects remotely:
       REMOTE_PDB_PORT=4444 rpdb script.py [args]
       REMOTE_PDB_PORT=4444 rpdb -m modname  [args]

     What happens:
       * script starts, rpdb blocks waiting for mcp-pdb to connect
       * once connected: ALL pdb output goes to both terminal and socket
       * commands accepted from EITHER terminal stdin OR mcp-pdb (local first)

  2. In-source one-liner (correct frame + all locals visible):
       from mcp_pdb.rpdb import set_trace; set_trace()

  3. PYTHONBREAKPOINT (zero code changes if code calls breakpoint()):
       PYTHONBREAKPOINT=mcp_pdb.rpdb.set_trace python script.py

  4. --pdbcls on Python >= 3.11 (remote-only, no local terminal):
       python -m pdb --pdbcls=mcp_pdb.rpdb:Debugger script.py
       pytest --pdb --pdbcls=mcp_pdb.rpdb:Debugger

Env vars:  REMOTE_PDB_HOST (default 127.0.0.1)  REMOTE_PDB_PORT (default 4444)
"""
import os
import pdb as _pdb
import socket as _socket
import sys
from typing import IO, cast

from remote_pdb import RemotePdb as _RemotePdb

_HOST = os.environ.get("REMOTE_PDB_HOST", "127.0.0.1")
_PORT = int(os.environ.get("REMOTE_PDB_PORT", "4444"))


# ── modes 2 / 3 ───────────────────────────────────────────────────────────────

def set_trace(host: str = _HOST, port: int = _PORT) -> None:
    """Drop-in for pdb.set_trace(). Blocks until a client connects."""
    _RemotePdb(host, port).set_trace(frame=sys._getframe().f_back)


# ── mode 4  (Python >= 3.11 --pdbcls, remote-only) ───────────────────────────

class Debugger(_RemotePdb):
    """Absorbs pdb.Pdb __init__ kwargs; uses REMOTE_PDB_HOST/PORT."""
    def __init__(self, **_: object) -> None:
        _RemotePdb.__init__(self, host=_HOST, port=_PORT)


# ── mode 1  helpers ───────────────────────────────────────────────────────────

class _TeeOutput:
    """Write PDB output to both local stdout and remote socket simultaneously."""

    def __init__(self, local: IO[str], conn: _socket.socket) -> None:
        self._local = local
        self._conn = conn
        self._conn_alive = True

    def write(self, data: str) -> None:
        self._local.write(data)
        self._local.flush()
        if self._conn_alive:
            try:
                self._conn.sendall(data.encode("utf-8", errors="replace"))
            except OSError:
                self._conn_alive = False  # stop trying after first failure

    def flush(self) -> None:
        self._local.flush()

    def fileno(self) -> int:
        try:
            return self._local.fileno()
        except Exception:
            return -1

    @property
    def encoding(self) -> str:
        return getattr(self._local, "encoding", "utf-8")


class _SelectStdin:
    """Read PDB commands from whichever of local stdin / socket is ready first.

    Local stdin is preferred when both are simultaneously ready so the user
    can always override what mcp-pdb is doing.  Remote commands are echoed to
    stdout so the user can see them in their terminal.
    """

    def __init__(self, local: IO[str], sock_file: IO[str]) -> None:
        self._local = local
        self._remote = sock_file
        self._fds: list[IO[str]] = [local, sock_file]
        # Drain any bytes buffered in stdin before the debugger starts
        # (e.g. Delete / arrow-key escape sequences left over from the shell).
        import select as _sel, os as _os
        try:
            if _sel.select([local], [], [], 0)[0]:
                _os.read(local.fileno(), 4096)
        except Exception:
            pass

    def readline(self) -> str:
        # pdb._runscript() clears __main__.__dict__ before starting the
        # debuggee.  Re-import here so names are always available regardless
        # of whether this module is __main__ or an installed package.
        import select as _sel
        import sys as _sys

        while self._fds:
            try:
                # Use a timeout so KeyboardInterrupt (SIGINT) can propagate.
                # select() with no timeout is auto-restarted by Python 3 after
                # SIGINT (PEP 475), making Ctrl+C impossible to deliver.
                ready, _, _ = _sel.select(self._fds, [], [], 0.5)
            except (ValueError, OSError):
                # stdin not selectable (e.g. redirected); drop it
                self._fds = [f for f in self._fds if f is not self._local]
                continue

            if not ready:
                continue  # timeout — loop back, allowing pending signals to fire

            # Prefer local stdin if both are ready
            src = self._local if self._local in ready else ready[0]
            line = src.readline()
            if line:
                if src is self._remote:
                    # Echo remote commands to local terminal
                    out = _sys.__stdout__ or _sys.stdout
                    out.write(line)
                    out.flush()
                return line

            # EOF on this source.
            # If local stdin closed (Ctrl+D), propagate EOF to PDB immediately
            # rather than continuing to wait on the remote socket.
            if src is self._local:
                self._fds = []
            else:
                self._fds = [f for f in self._fds if f is not src]

        return ""

    def fileno(self) -> int:
        try:
            return self._local.fileno()
        except Exception:
            return -1

    @property
    def encoding(self) -> str:
        return getattr(self._local, "encoding", "utf-8")


# ── mode 1  DualPdb ───────────────────────────────────────────────────────────

class DualPdb(_pdb.Pdb):
    """PDB that serves both the local terminal and a remote mcp-pdb client.

    On construction this blocks until a TCP client connects, then sets up:
      - stdout → tee to both local terminal and socket
      - stdin  → select() across local terminal and socket (local preferred)

    Usage:
        REMOTE_PDB_PORT=4444 rpdb script.py
        # In a second window / mcp-pdb:
        connect_remote_debug(host="127.0.0.1", port=4444)
    """

    def __init__(self, host: str = _HOST, port: int = _PORT, **_: object) -> None:
        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, True)
        srv.bind((host, port))
        print(f"rpdb: waiting for mcp connection on {host}:{port} ...",
              file=sys.__stderr__, flush=True)
        srv.listen(1)
        self._conn, addr = srv.accept()
        srv.close()
        print(f"rpdb: mcp connected from {addr}", file=sys.__stderr__, flush=True)

        self._sock_file: IO[str] = self._conn.makefile("r")

        # sys.__stdin__ / sys.__stdout__ can be None if streams were replaced;
        # fall back to the live sys.stdin / sys.stdout in that case.
        local_in:  IO[str] = sys.__stdin__  or sys.stdin
        local_out: IO[str] = sys.__stdout__ or sys.stdout

        super().__init__(
            stdin=cast(IO[str],  _SelectStdin(local_in,  self._sock_file)),
            stdout=cast(IO[str], _TeeOutput(local_out, self._conn)),
        )

    def set_trace(self, frame=None):  # type: ignore[override]
        if frame is None:
            frame = sys._getframe().f_back
        # Use super() rather than _pdb.Pdb to avoid a NameError if
        # __main__.__dict__ has been cleared by pdb._runscript().
        super().set_trace(frame)

    def do_quit(self, arg: str) -> bool | None:  # type: ignore[override]
        """Close the remote socket before handing off to pdb's quit handler."""
        try:
            self._sock_file.close()
        except Exception:
            pass
        try:
            self._conn.close()
        except Exception:
            pass
        return super().do_quit(arg)

    do_q = do_exit = do_quit  # type: ignore[assignment]


# ── entry point ───────────────────────────────────────────────────────────────

_HELP = """\
usage: rpdb [-c command] ... [-m module | pyfile] [arg] ...

rpdb — dual-session debugger: local terminal + remote mcp-pdb client.

rpdb waits for the mcp-pdb agent (or any TCP client) to connect, then
starts the script.  All PDB output goes to both the local terminal and
the remote client.  Commands are accepted from either source; the local
terminal takes priority.

Examples:
  REMOTE_PDB_PORT=4444 rpdb script.py [args]
  REMOTE_PDB_PORT=4444 rpdb -m mymodule [args]
  REMOTE_PDB_PORT=4444 rpdb -c 'b 42' script.py

Then connect the mcp-pdb agent:
  connect_remote_debug(host="127.0.0.1", port=4444)

Environment variables:
  REMOTE_PDB_HOST   Interface to listen on  (default: 127.0.0.1)
  REMOTE_PDB_PORT   TCP port                (default: 4444)

Flags:
  -c command        Execute 'command' as if given in .pdbrc
  -m module         Debug a module (like python -m module)
  -h, --help        Show this message and exit
"""


def main() -> None:
    """Console-script entry point for the ``rpdb`` command.

    Patches pdb.Pdb with DualPdb so that pdb.main() (which handles all
    argument parsing: -m, -c, etc.) instantiates DualPdb.
    Works on Python 3.10 — no --pdbcls flag needed.
    """
    import sys as _sys

    if "-h" in _sys.argv[1:] or "--help" in _sys.argv[1:]:
        print(_HELP, end="")
        raise SystemExit(0)

    # Fix the argv[0] so pdb's internal error messages say 'rpdb' not 'pdb.py'
    _sys.argv[0] = "rpdb"
    _pdb.Pdb = DualPdb  # type: ignore[misc]
    _pdb.main()


if __name__ == "__main__":
    main()
