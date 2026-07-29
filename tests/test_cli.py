import io
from pathlib import Path

from ariadne.cli import _ProgressBar, main
from ariadne.generation import ProgressEvent
from ariadne.llm import ModelResponse
from ariadne.models import LogicalModule


class CliFakeBackend:
    async def generate(self, request):
        return ModelResponse("# CLI Module\n\n## Summary\n\nGenerated.", "fake")


class CliInvalidBackend:
    async def generate(self, request):
        return ModelResponse("# One\n\n# Two\n", "fake")


def test_bare_command_prints_usage_without_error(capsys) -> None:
    status = main([])

    captured = capsys.readouterr()
    assert status == 0, captured.err
    assert captured.out == "usage: ariadne [-h] {inspect,weave,clean} ...\n"
    assert captured.err == ""


def test_inspect_command_defaults_to_names_only(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")

    status = main(["inspect", "--root", str(tmp_path), "--no-git"])

    captured = capsys.readouterr()
    assert status == 0
    assert "`-- repository" in captured.out
    assert "path=" not in captured.out
    assert "languages=" not in captured.out
    assert "Ignored" not in captured.out
    assert "Documentation files to generate: 2" in captured.out
    assert captured.err == ""


def test_inspect_verbosity_levels(tmp_path: Path, capsys) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("", encoding="utf-8")
    (tmp_path / "ignored.txt").write_text("", encoding="utf-8")

    assert main(["inspect", "--root", str(tmp_path), "--no-git", "-v"]) == 0
    low = capsys.readouterr().out
    assert "path=" in low
    assert "Ignored directories:" in low
    assert "node_modules [default-ignore]" in low
    assert "ignored.txt" not in low
    assert "languages=" not in low

    assert main(["inspect", "--root", str(tmp_path), "--no-git", "-vv"]) == 0
    high = capsys.readouterr().out
    assert "Repository:" in high
    assert "languages=Python" in high
    assert "Ignored paths:" in high
    assert "node_modules [default-ignore]" in high


def test_inspect_command_reports_invalid_selection(
    tmp_path: Path, capsys
) -> None:
    status = main(
        ["inspect", "missing", "--root", str(tmp_path), "--no-git"]
    )

    assert status == 2
    assert "inspection path does not exist" in capsys.readouterr().err


def test_weave_command_uses_injected_model_and_writes_document(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")

    status = main(
        [
            "weave", str(tmp_path / "src"), "--root", str(tmp_path), "--no-git",
            "--include-untracked", "--module-only",
        ],
        backend=CliFakeBackend(),
    )

    captured = capsys.readouterr()
    assert status == 0, captured.err
    destination = tmp_path / "src" / "src-genai-doc.md"
    assert destination.is_file()
    assert str(destination) in captured.out
    assert (tmp_path / ".ariadne" / "config.yaml").is_file()
    assert ".ariadne/" in (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "created default configuration" in captured.err
    assert "weaving [------------------------] 0/1 (0%)" in captured.err
    assert "weaving [########################] 1/1 (100%)" in captured.err
    assert " elapsed, " in captured.err
    assert " remaining]" in captured.err
    assert "\r" in captured.err
    assert "weave complete: generated=1" in captured.err
    assert "elapsed=" in captured.err


def test_clean_command_dry_run_then_removes_generated_document(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "src").mkdir()
    destination = tmp_path / "src" / "src-genai-doc.md"
    status = main(
        [
            "weave", str(tmp_path / "src"), "--root", str(tmp_path), "--no-git",
            "--include-untracked", "--module-only",
        ],
        backend=CliFakeBackend(),
    )
    assert status == 0
    capsys.readouterr()

    status = main(
        [
            "clean", str(tmp_path / "src"), "--root", str(tmp_path), "--no-git",
            "--dry-run",
        ]
    )
    preview = capsys.readouterr()
    assert status == 0
    assert str(destination) in preview.out
    assert "would remove 1 generated artifact(s)" in preview.err
    assert destination.exists()

    status = main(
        [
            "clean", str(tmp_path / "src"), "--root", str(tmp_path), "--no-git",
        ]
    )
    removed = capsys.readouterr()
    assert status == 0
    assert str(destination) in removed.out
    assert "removed 1 generated artifact(s)" in removed.err
    assert not destination.exists()


def test_weave_returns_partial_failure_exit_code(tmp_path: Path, capsys) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")

    status = main(
        [
            "weave", str(tmp_path / "src"), "--root", str(tmp_path), "--no-git",
            "--include-untracked", "--module-only",
        ],
        backend=CliInvalidBackend(),
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "partial=1" in captured.err
    assert "validation" in captured.err


def test_progress_bar_updates_in_place_with_elapsed_time_and_eta() -> None:
    current = 0.0
    stream = io.StringIO()
    progress = _ProgressBar(clock=lambda: current, stream=stream)
    module = LogicalModule("src", "src")

    progress.update(ProgressEvent(0, 2, module, "pending"))
    current = 10.0
    progress.update(ProgressEvent(1, 2, module, "generated"))
    elapsed = progress.finish()

    rendered = stream.getvalue()
    assert rendered.count("\r") == 2
    assert rendered.count("\n") == 1
    assert "0/2" in rendered
    assert "[00:10 elapsed, 00:10 remaining]" in rendered
    assert elapsed == 10.0
