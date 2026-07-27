from pathlib import Path

from ariadne.cli import main


def test_inspect_command_prints_hierarchy_without_git(
    tmp_path: Path, capsys
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("VALUE = 1\n", encoding="utf-8")

    status = main(["inspect", "--root", str(tmp_path), "--no-git"])

    captured = capsys.readouterr()
    assert status == 0
    assert "Logical modules:" in captured.out
    assert "languages=Python" in captured.out
    assert captured.err == ""


def test_inspect_command_reports_invalid_selection(
    tmp_path: Path, capsys
) -> None:
    status = main(
        ["inspect", "missing", "--root", str(tmp_path), "--no-git"]
    )

    assert status == 2
    assert "inspection path does not exist" in capsys.readouterr().err
