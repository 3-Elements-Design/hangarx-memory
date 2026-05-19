"""Tests for the ``hermes hangarx-memory docker`` subcommand.

These verify argparse wiring + the compose file discovery / docker
availability checks. We don't actually start docker — that's a
manual integration test the user runs once when they install the
plugin.
"""
from __future__ import annotations

import argparse
import shutil
from pathlib import Path

import pytest

from hangarx_memory.cli import (
    _cmd_docker,
    _docker_available,
    _find_compose_file,
    register_cli,
)

# ---------------------------------------------------------------------------
# Compose file discovery
# ---------------------------------------------------------------------------


class TestFindComposeFile:
    def test_finds_bundled_file(self) -> None:
        """The package-root docker-compose.cortex.yml ships with the install."""
        path = _find_compose_file()
        assert path is not None
        assert path.name == "docker-compose.cortex.yml"
        assert path.is_file()

    def test_returns_none_when_nothing_is_findable(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Move HERMES_HOME to an empty dir so the fallback finds nothing.
        # We also pretend __file__ lives somewhere with no sibling
        # docker-compose.cortex.yml — easiest way is to monkey-patch the
        # candidates list, but since the bundled file always exists in
        # this checkout we use a different approach: re-import the
        # module pointing __file__ elsewhere.
        # Simpler: just monkey-patch ``Path.is_file`` for the duration of
        # the test so every candidate misses.
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(Path, "is_file", lambda self: False)
        assert _find_compose_file() is None


# ---------------------------------------------------------------------------
# Docker availability probe
# ---------------------------------------------------------------------------


class TestDockerAvailable:
    def test_missing_binary_returns_clear_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: None)
        ok, msg = _docker_available()
        assert ok is False
        assert "Install Docker" in msg or "docker command not found" in msg

    def test_daemon_unreachable_returns_clear_error(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/local/bin/docker")
        # Stub subprocess.run to simulate a non-zero return from docker info.
        import subprocess as _sp

        class _FakeCompleted:
            returncode = 1
            stdout = ""
            stderr = "Cannot connect to the Docker daemon"

        monkeypatch.setattr(_sp, "run", lambda *a, **kw: _FakeCompleted())
        ok, msg = _docker_available()
        assert ok is False
        assert "daemon" in msg.lower()


# ---------------------------------------------------------------------------
# Subcommand dispatch
# ---------------------------------------------------------------------------


class TestSubcommandDispatch:
    def _parse(self, *argv: str) -> argparse.Namespace:
        parser = argparse.ArgumentParser()
        register_cli(parser)
        return parser.parse_args(list(argv))

    def test_path_prints_compose_path(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = self._parse("docker", "path")
        rc = _cmd_docker(args)
        captured = capsys.readouterr()
        assert rc == 0
        assert "docker-compose.cortex.yml" in captured.out

    def test_unknown_subcommand_returns_error(
        self,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # docker_subcommand=None reaches the help/error fallback.
        # We need to bypass the docker_available check to hit it.
        monkeypatch.setattr(
            "hangarx_memory.cli._docker_available",
            lambda: (True, "1.0"),
        )
        # Simulate parsing without a subcommand selected. argparse
        # leaves docker_subcommand=None which means status (default).
        # To exercise the unknown branch explicitly we craft an args
        # namespace by hand.
        args = argparse.Namespace(docker_subcommand="bogus")
        rc = _cmd_docker(args)
        captured = capsys.readouterr()
        assert rc == 1
        assert "Usage:" in captured.err

    @pytest.mark.parametrize("sub", ["up", "down", "logs", "pull"])
    def test_subcommand_fails_fast_without_docker(
        self,
        sub: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            "hangarx_memory.cli._docker_available",
            lambda: (False, "docker not installed"),
        )
        args = argparse.Namespace(docker_subcommand=sub, follow=False, service=None, purge=False)
        rc = _cmd_docker(args)
        captured = capsys.readouterr()
        assert rc == 2
        assert "docker not installed" in captured.err


# ---------------------------------------------------------------------------
# CLI parser wiring
# ---------------------------------------------------------------------------


class TestParserRegistration:
    def test_docker_subparser_registered(self) -> None:
        parser = argparse.ArgumentParser()
        register_cli(parser)
        # All six subcommands should parse cleanly.
        for sub in ("up", "down", "status", "logs", "pull", "path"):
            args = parser.parse_args(["docker", sub])
            assert args.hangarx_memory_command == "docker"
            assert args.docker_subcommand == sub

    def test_logs_follow_flag(self) -> None:
        parser = argparse.ArgumentParser()
        register_cli(parser)
        args = parser.parse_args(["docker", "logs", "-f", "cortex-api"])
        assert args.follow is True
        assert args.service == "cortex-api"

    def test_down_purge_flag(self) -> None:
        parser = argparse.ArgumentParser()
        register_cli(parser)
        args = parser.parse_args(["docker", "down", "--purge"])
        assert args.purge is True
