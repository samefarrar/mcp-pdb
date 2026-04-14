# MCP-PDB: Python Debugger Interface for Claude/LLMs

![Python](https://img.shields.io/badge/Python-3.12+-blue.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

MCP-PDB provides tools for using Python's debugger (pdb) with Claude and other LLMs through the Model Context Protocol (MCP). This was inspired by [debug-gym](https://microsoft.github.io/debug-gym/) by Microsoft, which showed gains in various coding benchmarks by providing a coding agent access to a python debugger.

## ⚠️ Security Warning

This tool executes Python code through the debugger. Use in trusted environments only.

## Installation

Works best with [uv](https://docs.astral.sh/uv/getting-started/features/)

### Claude Code
```bash
# Install the MCP server
claude mcp add mcp-pdb -- uv run --with mcp-pdb mcp-pdb

# Alternative: Install with specific Python version
claude mcp add mcp-pdb -- uv run --python 3.13 --with mcp-pdb mcp-pdb

# Note: The -- separator is required for Claude Code CLI
```

### Windsurf
```json
{
  "mcpServers": {
    "mcp-pdb": {
      "command": "uv",
      "args": [
        "run",
        "--with",
        "mcp-pdb",
        "mcp-pdb"
      ]
    }
  }
}
```

## Available Tools

| Tool | Description |
|------|-------------|
| `start_debug(file_path, use_pytest, args)` | Start a local debugging session for a Python file |
| `connect_remote_debug(host, port, timeout)` | Connect to a remote PDB session over TCP |
| `send_pdb_command(command)` | Send a command to the running PDB instance (local or remote) |
| `set_breakpoint(file_path, line_number)` | Set a breakpoint at a specific line |
| `clear_breakpoint(file_path, line_number)` | Clear a breakpoint at a specific line |
| `list_breakpoints()` | List all current breakpoints |
| `restart_debug()` | Restart/reconnect the current debugging session |
| `examine_variable(variable_name)` | Get detailed information about a variable |
| `get_debug_status()` | Show the current state of the debugging session |
| `end_debug()` | End the current debugging session (closes socket for remote) |

## Remote Debugging

Connect to a Python process that exposes PDB over a TCP socket.  All existing
tools (`send_pdb_command`, `set_breakpoint`, `examine_variable`, etc.) work
identically once connected.

### rpdb — dual-session debugger (recommended)

`rpdb` is installed alongside `mcp-pdb` and is the easiest way to debug with
both a local terminal and the MCP agent simultaneously.

```
Terminal ──→ stdin ──→ PDB ──→ stdout ──→ Terminal
                            └──→ socket ──→ mcp-pdb
mcp-pdb  ──→ socket ──┘  (local stdin takes priority)
```

**Start a script under rpdb:**

```bash
REMOTE_PDB_PORT=4444 rpdb script.py [args]
REMOTE_PDB_PORT=4444 rpdb -m mymodule [args]
```

rpdb prints `waiting for mcp connection on 127.0.0.1:4444 ...` and blocks
until the MCP agent connects.  All normal pdb flags (`-c`, `--help`, etc.)
are supported.

**Then connect from Claude / the MCP agent:**

```
connect_remote_debug(host="127.0.0.1", port=4444)
```

All PDB output is now mirrored to both the terminal and the agent.  Commands
typed in the terminal take priority; the agent's commands are echoed to the
terminal so you can follow along.

**Environment variables:**

| Variable | Default | Description |
|----------|---------|-------------|
| `REMOTE_PDB_HOST` | `127.0.0.1` | Interface to listen on (`0.0.0.0` for remote machines) |
| `REMOTE_PDB_PORT` | `4444` | TCP port |

**In-source one-liner** (correct frame, all locals visible):

```python
# Add at the line you want to break at:
from mcp_pdb.rpdb import set_trace; set_trace()
```

The process blocks here until the MCP agent (or any TCP client) connects.

**Zero-code-change with `breakpoint()`:**

```bash
PYTHONBREAKPOINT=mcp_pdb.rpdb.set_trace python script.py
```

**Python ≥ 3.11 with `--pdbcls`** (remote only, no local terminal):

```bash
python -m pdb --pdbcls=mcp_pdb.rpdb:Debugger script.py
pytest --pdb --pdbcls=mcp_pdb.rpdb:Debugger
```

### Manual remote-pdb setup

If you need to attach to an already-running process without using `rpdb`:

**Option A – `remote-pdb` package**

```python
from remote_pdb import RemotePdb
RemotePdb("0.0.0.0", 4444).set_trace()
```

**Option B – stdlib only**

```python
import io, socket, pdb

srv = socket.socket()
srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("0.0.0.0", 4444))
srv.listen(1)
conn, _ = srv.accept()   # blocks until MCP connects
f = conn.makefile("rwb", buffering=0)
pdb.Pdb(stdin=io.TextIOWrapper(f), stdout=io.TextIOWrapper(f)).set_trace()
```

**Connect from Claude / the LLM:**

```
connect_remote_debug(host="192.168.1.10", port=4444)
```

`timeout` (default 30 s) controls how long to retry on `ConnectionRefused`—
useful when the remote process has not yet reached `set_trace()`.

`restart_debug()` reconnects to the same host/port.  `end_debug()` closes the
socket gracefully (sends `q` first).

## Common PDB Commands

| Command | Description |
|---------|-------------|
| `n` | Next line (step over) |
| `s` | Step into function |
| `c` | Continue execution |
| `r` | Return from current function |
| `p variable` | Print variable value |
| `pp variable` | Pretty print variable |
| `b file:line` | Set breakpoint |
| `cl num` | Clear breakpoint |
| `l` | List source code |
| `q` | Quit debugging |

## Features

- Project-aware debugging with automatic virtual environment detection
- Support for both direct Python debugging and pytest-based debugging
- Automatic breakpoint tracking and restoration between sessions
- Works with UV package manager
- Variable inspection with type information and attribute listing

## Troubleshooting

### Claude Code Installation Issues

If you encounter an error like:
```
MCP server "mcp-pdb" Connection failed: spawn /Users/xxx/.local/bin/uv run --python 3.13 --with mcp-pdb mcp-pdb ENOENT
```

Make sure to include the `--` separator when using `claude mcp add`:
```bash
# ✅ Correct
claude mcp add mcp-pdb -- uv run --with mcp-pdb mcp-pdb

# ❌ Incorrect (missing --)
claude mcp add mcp-pdb uv run --with mcp-pdb mcp-pdb
```

To verify your installation:
```bash
# Check if mcp-pdb is listed
claude mcp list | grep mcp-pdb

# Check server status in Claude Code
# Type /mcp in Claude Code to see connection status
```

## License

MIT License - See LICENSE file for details.
