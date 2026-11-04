# mcp-pdb Development Guide

## Project Overview
This project is a debugging tool that integrates PDB (Python Debugger) with the MCP (Multi-modal Chat Protocol) framework. It allows interactive debugging of Python code through a chat interface.

## Build Commands
- Run the server: `uv run mcp-pdb`
- Lint: `uv run ruff check`
- Type check: `uv run pyright`

## Development Environment

### Package Management
- This project uses `uv` for package management instead of pip/pip-tools
- Commands should use the `uv run` prefix for running Python tools
- A project is treated as uv-managed when `uv` is on PATH and the project root has a `pyproject.toml`

### Debugging
- When debugging with `uv`, use `uv run pdb <file_path>` instead of `python -m pdb`
- When testing with pytest, use `uv run pytest --pdb <file_path>`

### Project Structure
- Main functionality is in `src/mcp_pdb/main.py`
- The code provides MCP tools for:
  - Starting/stopping debug sessions
  - Setting/managing breakpoints
  - Sending commands to PDB
  - Examining variables
  - Managing debug status

### Development Notes
- Built on the `mcp` 2.x `MCPServer`; sync tools run in worker threads, so every tool (except `interrupt`) holds `_session_lock`
- Never `print()` in `main.py`: stdout is the MCP JSON-RPC channel. Use `log` (stderr); a test enforces this
- Breakpoints are tracked across debug sessions
