from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from .models import FilePolicy


class GitError(RuntimeError):
    pass


@dataclass(frozen=True)
class GitIndex:
    tracked: frozenset[str]
    untracked: frozenset[str]
    ignored: frozenset[str]

    def status(self, path: str) -> str | None:
        if path in self.tracked:
            return "tracked"
        if path in self.untracked:
            return "untracked"
        if self.is_ignored(path):
            return "ignored"
        return None

    def is_ignored(self, path: str) -> bool:
        return any(path == item or path.startswith(item.rstrip("/") + "/") for item in self.ignored)

    def policy_reason(
        self,
        path: str,
        policy: FilePolicy,
        *,
        respect_gitignore: bool,
    ) -> str | None:
        if policy is FilePolicy.TRACKED_ONLY and path not in self.tracked:
            return "git-untracked" if not self.is_ignored(path) else "gitignore"
        if policy is FilePolicy.TRACKED_AND_UNTRACKED:
            if not respect_gitignore:
                return None
            if path not in self.tracked and path not in self.untracked:
                return "gitignore" if self.is_ignored(path) else "git-unlisted"
        return None


def read_git_index(root: Path) -> GitIndex:
    return GitIndex(
        tracked=frozenset(_git_paths(root, ["ls-files", "--cached", "-z"])),
        untracked=frozenset(
            _git_paths(root, ["ls-files", "--others", "--exclude-standard", "-z"])
        ),
        ignored=frozenset(
            _git_paths(
                root,
                ["ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
            )
        ),
    )


def _git_paths(root: Path, arguments: list[str]) -> list[str]:
    result = subprocess.run(
        ["git", "-c", "safe.directory=*", "-C", str(root), *arguments],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        message = result.stderr.decode(errors="replace").strip()
        raise GitError(message or "Git command failed")
    return [
        item.decode(errors="surrogateescape").replace("\\", "/").rstrip("/")
        for item in result.stdout.split(b"\0")
        if item
    ]
