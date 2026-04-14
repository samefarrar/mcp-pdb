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

### Setup on the remote side

**Option A – `remote-pdb` package (recommended)**

```python
# In your Python script or inside a debugger trigger:
from remote_pdb import RemotePdb
RemotePdb("0.0.0.0", 4444).set_trace()
```

Install once: `pip install remote-pdb`

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

### Connect from Claude / the LLM

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
