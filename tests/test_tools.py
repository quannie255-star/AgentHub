"""Unit tests for built-in Tools layer.

Covers:
  - Diff Viewer: compute_diff, diff_files, language detection, size limits
  - Web Preview: server start/stop, port allocation, error cases
  - Deploy: docker compose wrapper, missing file, missing binary
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from src.core.schema import DeployResult, DiffResult, PreviewResult
from src.tools.deploy import DeployManager
from src.tools.diff_viewer import (
    DEFAULT_MAX_FILE_SIZE,
    _detect_language,
    compute_diff,
    diff_files,
)
from src.tools.preview import PreviewServer


# ======================================================================
# Diff Viewer
# ======================================================================

class TestComputeDiff:
    def test_basic_diff(self):
        result = compute_diff(
            original="line1\nline2\nline3\n",
            modified="line1\nline2_changed\nline3\n",
            file_path="test.py",
        )
        assert isinstance(result, DiffResult)
        assert "@@" in result.unified_diff
        assert "-line2" in result.unified_diff or " line2" in result.unified_diff
        assert result.file_path == "test.py"

    def test_addition(self):
        result = compute_diff(
            original="line1\n",
            modified="line1\nline2\n",
            file_path="add.py",
        )
        assert "+line2" in result.unified_diff

    def test_deletion(self):
        result = compute_diff(
            original="line1\nline2\n",
            modified="line1\n",
            file_path="del.py",
        )
        assert "-line2" in result.unified_diff

    def test_no_changes(self):
        result = compute_diff(
            original="same\n",
            modified="same\n",
        )
        assert result.unified_diff == "" or result.unified_diff.strip() == ""

    def test_language_auto_detect(self):
        result = compute_diff(
            original="x", modified="y", file_path="app.js"
        )
        assert result.language == "javascript"

    def test_language_explicit_override(self):
        result = compute_diff(
            original="x", modified="y", file_path="foo.xyz", language="custom"
        )
        assert result.language == "custom"

    def test_empty_inputs(self):
        result = compute_diff(original="", modified="hello\n", file_path="new.txt")
        assert isinstance(result, DiffResult)
        assert result.original == ""
        assert result.modified == "hello\n"

    def test_context_lines(self):
        result = compute_diff(
            original="a\nb\nc\nd\ne\nf\ng\n",
            modified="a\nb\nCHANGED\nd\ne\nf\ng\n",
            context_lines=1,
        )
        # With context=1, should see b and d but not a, e, f, g
        diff = result.unified_diff
        # Only 3 lines around the change should appear
        assert " a" not in diff or " e" not in diff

    def test_no_filename_defaults(self):
        result = compute_diff(original="old", modified="new")
        assert "original" in result.unified_diff or "modified" in result.unified_diff


class TestDiffFiles:
    def test_diff_two_files(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f1, tempfile.NamedTemporaryFile(
            mode="w", suffix=".py", delete=False, encoding="utf-8"
        ) as f2:
            f1.write("print('old')\n")
            f1.flush()
            f2.write("print('new')\n")
            f2.flush()
            p1, p2 = f1.name, f2.name

        try:
            result = diff_files(p1, p2)
            assert result.file_path == p1
            assert result.language == "python"
            assert "-print('old')" in result.unified_diff
            assert "+print('new')" in result.unified_diff
        finally:
            Path(p1).unlink(missing_ok=True)
            Path(p2).unlink(missing_ok=True)

    def test_file_not_found(self):
        with pytest.raises(FileNotFoundError):
            diff_files("/nonexistent/a.txt", "/nonexistent/b.txt")

    def test_file_too_large(self):
        """Create a file larger than max_size and verify it's rejected."""
        with tempfile.NamedTemporaryFile(
            mode="wb", suffix=".txt", delete=False
        ) as f:
            f.write(b"x" * (DEFAULT_MAX_FILE_SIZE + 1))
            f.flush()
            large_path = f.name

        try:
            with pytest.raises(ValueError, match="exceeds max size"):
                diff_files(large_path, large_path, max_size=100)
        finally:
            Path(large_path).unlink(missing_ok=True)


class TestLanguageDetection:
    def test_python(self):
        assert _detect_language("app.py") == "python"

    def test_javascript(self):
        assert _detect_language("app.js") == "javascript"

    def test_typescript(self):
        assert _detect_language("app.ts") == "typescript"
        assert _detect_language("app.tsx") == "tsx"

    def test_dockerfile(self):
        assert _detect_language("Dockerfile") == "dockerfile"

    def test_yaml(self):
        assert _detect_language("config.yml") == "yaml"
        assert _detect_language("config.yaml") == "yaml"

    def test_unknown(self):
        assert _detect_language("file.unknown_ext") == ""

    def test_no_extension(self):
        assert _detect_language("Makefile") == ""


# ======================================================================
# Web Preview
# ======================================================================

class TestPreviewServer:
    @pytest.fixture
    def tmp_dir(self) -> str:
        with tempfile.TemporaryDirectory() as d:
            # Create a sample index.html
            index = Path(d) / "index.html"
            index.write_text("<html><body><h1>Hello</h1></body></html>", encoding="utf-8")
            yield d

    def test_start_stop(self, tmp_dir: str):
        server = PreviewServer()
        result = server.start(tmp_dir)
        assert result.status == "running"
        assert "localhost" in result.url
        assert result.port >= 9000
        assert server.is_running

        stopped = server.stop()
        assert stopped.status == "stopped"
        assert not server.is_running

    def test_start_with_explicit_port(self, tmp_dir: str):
        server = PreviewServer()
        result = server.start(tmp_dir, port=9123)
        assert result.port == 9123
        server.stop()

    def test_start_nonexistent_dir_raises(self):
        server = PreviewServer()
        with pytest.raises(FileNotFoundError):
            server.start("/nonexistent/directory/path")

    def test_custom_port_range(self, tmp_dir: str):
        server = PreviewServer(port_range=(9500, 9505))
        result = server.start(tmp_dir)
        assert 9500 <= result.port <= 9505
        server.stop()

    def test_multiple_servers_different_ports(self, tmp_dir: str):
        s1 = PreviewServer(port_range=(9600, 9605))
        s2 = PreviewServer(port_range=(9610, 9615))

        r1 = s1.start(tmp_dir)
        r2 = s2.start(tmp_dir)

        assert r1.port != r2.port

        s1.stop()
        s2.stop()


# ======================================================================
# Deploy
# ======================================================================

class TestDeployManager:
    async def test_up_missing_file(self):
        dm = DeployManager(compose_path="/nonexistent/docker-compose.yml")
        result = await dm.up()
        assert result.status == "failed"
        assert "not found" in result.log.lower()

    async def test_down_missing_file(self):
        dm = DeployManager(compose_path="/nonexistent/docker-compose.yml")
        result = await dm.down()
        assert result.status == "failed"

    async def test_status_missing_file(self):
        dm = DeployManager(compose_path="/nonexistent/docker-compose.yml")
        result = await dm.status()
        assert result.status == "failed"

    async def test_up_with_existing_compose_file(self):
        """When docker-compose.yml exists, it should attempt to run."""
        with tempfile.TemporaryDirectory() as d:
            compose_file = Path(d) / "docker-compose.yml"
            compose_file.write_text("version: '3'\nservices:\n  web:\n    image: nginx\n")
            dm = DeployManager(compose_path=str(compose_file))
            result = await dm.up()
            # If docker is not installed, it returns "failed" with "Docker not found"
            # If docker is installed but compose fails for other reasons, also failed
            # Either way, we get a structured DeployResult
            assert isinstance(result, DeployResult)
            assert result.status in ("deployed", "failed")

    async def test_default_services(self):
        dm = DeployManager(
            compose_path="docker/docker-compose.yml",
            default_services=["backend", "frontend"],
        )
        assert dm._default_services == ["backend", "frontend"]

    async def test_constructor_resolves_path(self):
        dm = DeployManager(compose_path="docker/docker-compose.yml")
        assert "docker-compose.yml" in dm._compose_path
        assert Path(dm._compose_path).is_absolute()
