"""Tests for helper functions in mcp_pdb.main."""

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from mcp_pdb.main import find_project_root, find_venv_details, sanitize_arguments


class TestFindProjectRoot:
    """Tests for find_project_root function."""

    def test_finds_pyproject_toml(self, tmp_path):
        """Should find project root when pyproject.toml exists."""
        # Create nested directory structure
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        (project_dir / "pyproject.toml").touch()

        src_dir = project_dir / "src" / "package"
        src_dir.mkdir(parents=True)

        result = find_project_root(str(src_dir))
        assert result == str(project_dir)

    def test_finds_git_directory(self, tmp_path):
        """Should find project root when .git directory exists."""
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        (project_dir / ".git").mkdir()

        nested_dir = project_dir / "src" / "deep" / "nested"
        nested_dir.mkdir(parents=True)

        result = find_project_root(str(nested_dir))
        assert result == str(project_dir)

    def test_finds_setup_py(self, tmp_path):
        """Should find project root when setup.py exists."""
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        (project_dir / "setup.py").touch()

        src_dir = project_dir / "src"
        src_dir.mkdir()

        result = find_project_root(str(src_dir))
        assert result == str(project_dir)

    def test_finds_requirements_txt(self, tmp_path):
        """Should find project root when requirements.txt exists."""
        project_dir = tmp_path / "my_project"
        project_dir.mkdir()
        (project_dir / "requirements.txt").touch()

        src_dir = project_dir / "src"
        src_dir.mkdir()

        result = find_project_root(str(src_dir))
        assert result == str(project_dir)

    def test_fallback_to_start_path(self, tmp_path):
        """Should fallback to start path when no indicators found."""
        # Create directory with no project indicators
        empty_dir = tmp_path / "no_project" / "nested"
        empty_dir.mkdir(parents=True)

        result = find_project_root(str(empty_dir))
        assert result == str(empty_dir)

    def test_prefers_closest_indicator(self, tmp_path):
        """Should find the closest project root when multiple exist."""
        # Create nested projects
        outer_project = tmp_path / "outer"
        outer_project.mkdir()
        (outer_project / "pyproject.toml").touch()

        inner_project = outer_project / "inner"
        inner_project.mkdir()
        (inner_project / "pyproject.toml").touch()

        src_dir = inner_project / "src"
        src_dir.mkdir()

        result = find_project_root(str(src_dir))
        assert result == str(inner_project)


class TestFindVenvDetails:
    """Tests for find_venv_details function."""

    def test_finds_venv_in_project_root(self, tmp_path):
        """Should find .venv directory in project root."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Create venv structure
        venv_dir = project_dir / ".venv"
        if sys.platform == "win32":
            bin_dir = venv_dir / "Scripts"
            python_name = "python.exe"
        else:
            bin_dir = venv_dir / "bin"
            python_name = "python"

        bin_dir.mkdir(parents=True)
        (bin_dir / python_name).touch()

        python_exe, found_bin_dir = find_venv_details(str(project_dir))

        assert python_exe == str(bin_dir / python_name)
        assert found_bin_dir == str(bin_dir)

    def test_finds_venv_named_venv(self, tmp_path):
        """Should find venv directory named 'venv'."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        venv_dir = project_dir / "venv"
        if sys.platform == "win32":
            bin_dir = venv_dir / "Scripts"
            python_name = "python.exe"
        else:
            bin_dir = venv_dir / "bin"
            python_name = "python"

        bin_dir.mkdir(parents=True)
        (bin_dir / python_name).touch()

        python_exe, found_bin_dir = find_venv_details(str(project_dir))

        assert python_exe == str(bin_dir / python_name)
        assert found_bin_dir == str(bin_dir)

    def test_prefers_project_venv_over_virtual_env_variable(self, tmp_path):
        """Should prefer project's .venv over VIRTUAL_ENV environment variable.

        This is the key fix for issue #3 - when mcp-pdb runs under uv,
        VIRTUAL_ENV points to mcp-pdb's own environment, not the debuggee's.
        """
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Create project's venv
        project_venv = project_dir / ".venv"
        if sys.platform == "win32":
            project_bin = project_venv / "Scripts"
            python_name = "python.exe"
        else:
            project_bin = project_venv / "bin"
            python_name = "python"
        project_bin.mkdir(parents=True)
        (project_bin / python_name).touch()

        # Create a different venv that VIRTUAL_ENV points to (simulating uv's env)
        other_venv = tmp_path / "uv_env"
        if sys.platform == "win32":
            other_bin = other_venv / "Scripts"
        else:
            other_bin = other_venv / "bin"
        other_bin.mkdir(parents=True)
        (other_bin / python_name).touch()

        # Set VIRTUAL_ENV to the other venv
        with mock.patch.dict(os.environ, {"VIRTUAL_ENV": str(other_venv)}):
            python_exe, found_bin_dir = find_venv_details(str(project_dir))

        # Should find the project's venv, not the one from VIRTUAL_ENV
        assert python_exe == str(project_bin / python_name)
        assert found_bin_dir == str(project_bin)

    def test_falls_back_to_virtual_env_when_no_local_venv(self, tmp_path):
        """Should fall back to VIRTUAL_ENV when no local venv exists."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()
        # No venv in project

        # Create venv that VIRTUAL_ENV points to
        env_venv = tmp_path / "some_env"
        if sys.platform == "win32":
            env_bin = env_venv / "Scripts"
            python_name = "python.exe"
        else:
            env_bin = env_venv / "bin"
            python_name = "python"
        env_bin.mkdir(parents=True)
        (env_bin / python_name).touch()

        with mock.patch.dict(os.environ, {"VIRTUAL_ENV": str(env_venv)}, clear=False):
            # Also need to clear CONDA_PREFIX to avoid interference
            env_copy = os.environ.copy()
            env_copy["VIRTUAL_ENV"] = str(env_venv)
            env_copy.pop("CONDA_PREFIX", None)
            with mock.patch.dict(os.environ, env_copy, clear=True):
                python_exe, found_bin_dir = find_venv_details(str(project_dir))

        assert python_exe == str(env_bin / python_name)
        assert found_bin_dir == str(env_bin)

    def test_finds_venv_in_parent_directory(self, tmp_path):
        """Should find venv in parent directory."""
        parent_dir = tmp_path / "parent"
        parent_dir.mkdir()

        # Create venv in parent
        venv_dir = parent_dir / ".venv"
        if sys.platform == "win32":
            bin_dir = venv_dir / "Scripts"
            python_name = "python.exe"
        else:
            bin_dir = venv_dir / "bin"
            python_name = "python"
        bin_dir.mkdir(parents=True)
        (bin_dir / python_name).touch()

        # Project is a subdirectory
        project_dir = parent_dir / "project"
        project_dir.mkdir()

        python_exe, found_bin_dir = find_venv_details(str(project_dir))

        assert python_exe == str(bin_dir / python_name)
        assert found_bin_dir == str(bin_dir)

    def test_returns_none_when_no_venv_found(self, tmp_path):
        """Should return None, None when no venv is found."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Clear environment variables that might point to venvs
        with mock.patch.dict(os.environ, {}, clear=True):
            # Restore PATH for the test to work
            with mock.patch.dict(os.environ, {"PATH": ""}, clear=True):
                python_exe, bin_dir = find_venv_details(str(project_dir))

        assert python_exe is None
        assert bin_dir is None

    def test_prefers_dotenv_over_env(self, tmp_path):
        """Should check .venv before venv (order matters)."""
        project_dir = tmp_path / "project"
        project_dir.mkdir()

        # Create both .venv and venv
        for name in [".venv", "venv"]:
            venv_dir = project_dir / name
            if sys.platform == "win32":
                bin_dir = venv_dir / "Scripts"
                python_name = "python.exe"
            else:
                bin_dir = venv_dir / "bin"
                python_name = "python"
            bin_dir.mkdir(parents=True)
            (bin_dir / python_name).touch()

        python_exe, found_bin_dir = find_venv_details(str(project_dir))

        # Should find .venv first (it's first in the list)
        expected_venv = project_dir / ".venv"
        if sys.platform == "win32":
            expected_bin = expected_venv / "Scripts"
            python_name = "python.exe"
        else:
            expected_bin = expected_venv / "bin"
            python_name = "python"

        assert python_exe == str(expected_bin / python_name)


class TestSanitizeArguments:
    """Tests for sanitize_arguments function."""

    def test_parses_simple_arguments(self):
        """Should parse simple space-separated arguments."""
        result = sanitize_arguments("--flag value")
        assert result == ["--flag", "value"]

    def test_parses_quoted_arguments(self):
        """Should handle quoted arguments with spaces."""
        result = sanitize_arguments('--name "hello world"')
        assert result == ["--name", "hello world"]

    def test_parses_empty_string(self):
        """Should return empty list for empty string."""
        result = sanitize_arguments("")
        assert result == []

    def test_rejects_semicolon(self):
        """Should reject arguments containing semicolon."""
        with pytest.raises(ValueError, match="Invalid character"):
            sanitize_arguments("arg1; rm -rf /")

    def test_rejects_double_ampersand(self):
        """Should reject arguments containing &&."""
        with pytest.raises(ValueError, match="Invalid character"):
            sanitize_arguments("arg1 && malicious")

    def test_rejects_double_pipe(self):
        """Should reject arguments containing ||."""
        with pytest.raises(ValueError, match="Invalid character"):
            sanitize_arguments("arg1 || fallback")

    def test_rejects_backticks(self):
        """Should reject arguments containing backticks."""
        with pytest.raises(ValueError, match="Invalid character"):
            sanitize_arguments("`whoami`")

    def test_rejects_dollar_paren(self):
        """Should reject arguments containing $(."""
        with pytest.raises(ValueError, match="Invalid character"):
            sanitize_arguments("$(whoami)")

    def test_rejects_pipe(self):
        """Should reject arguments containing pipe."""
        with pytest.raises(ValueError, match="Invalid character"):
            sanitize_arguments("arg | grep something")

    def test_rejects_redirect_greater(self):
        """Should reject arguments containing >."""
        with pytest.raises(ValueError, match="Invalid character"):
            sanitize_arguments("arg > /etc/passwd")

    def test_rejects_redirect_less(self):
        """Should reject arguments containing <."""
        with pytest.raises(ValueError, match="Invalid character"):
            sanitize_arguments("arg < /etc/passwd")

    def test_parses_complex_valid_arguments(self):
        """Should parse complex but valid arguments."""
        result = sanitize_arguments('--config /path/to/config.json --verbose -n 5')
        assert result == ["--config", "/path/to/config.json", "--verbose", "-n", "5"]

    def test_handles_equals_in_arguments(self):
        """Should handle arguments with equals signs."""
        result = sanitize_arguments("--key=value --other=123")
        assert result == ["--key=value", "--other=123"]
