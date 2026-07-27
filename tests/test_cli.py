from pathlib import Path

from ariadne.cli import main
from ariadne.llm import ModelResponse


class CliFakeBackend:
    def generate(self, request):
        return ModelResponse("# CLI Module\n\n## Summary\n\nGenerated.", "fake")


def test_bare_command_prints_usage_without_error(capsys) -> None:
    status = main([])

    captured = capsys.readouterr()
    assert status == 0, captured.err
    assert captured.out == "usage: ariadne [-h] {inspect,weave} ...\n"
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
