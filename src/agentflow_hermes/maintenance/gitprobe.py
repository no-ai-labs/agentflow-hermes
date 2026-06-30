from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from agentflow_hermes.live.sanitize import safe_durable_ref


class GitExecutor(Protocol):
    def run(self, args: list[str], *, cwd: str | Path) -> str: ...


class SubprocessGitExecutor:
    """Read-only git executor used by the watcher.

    The mutating verbs intentionally are not exposed by GitProbe; fetch updates
    remote refs only and never touches the worktree.
    """

    def run(self, args: list[str], *, cwd: str | Path) -> str:
        proc = subprocess.run(args, cwd=str(cwd), text=True, capture_output=True, check=False)
        if proc.returncode != 0:
            raise RuntimeError(proc.stderr.strip() or proc.stdout.strip() or f"command failed: {args[0]}")
        return proc.stdout.strip()


@dataclass(frozen=True)
class GitProbeResult:
    repo_id: str
    upstream_sha: str
    behind: int
    ahead: int
    dirty: bool
    local_carried: bool
    ff_eligible: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "repo_id": self.repo_id,
            "upstream_sha": self.upstream_sha,
            "behind": self.behind,
            "ahead": self.ahead,
            "dirty": self.dirty,
            "local_carried": self.local_carried,
            "ff_eligible": self.ff_eligible,
        }


class GitProbe:
    def __init__(self, repo: str | Path, *, remote: str = "origin", branch: str = "main", executor: GitExecutor | None = None):
        self.repo = Path(repo)
        self.remote = remote
        self.branch = branch
        self.executor = executor or SubprocessGitExecutor()

    def probe(self, *, fetch: bool = True) -> GitProbeResult:
        if fetch:
            self.executor.run(["git", "fetch", "--quiet", self.remote], cwd=self.repo)
        upstream_ref = f"{self.remote}/{self.branch}"
        upstream_sha = self.executor.run(["git", "rev-parse", upstream_ref], cwd=self.repo)
        counts = self.executor.run(["git", "rev-list", "--left-right", "--count", f"{upstream_ref}...HEAD"], cwd=self.repo)
        left, right = _parse_counts(counts)
        status = self.executor.run(["git", "status", "--porcelain"], cwd=self.repo)
        dirty = bool(status.strip())
        ahead = right
        behind = left
        local_carried = ahead > 0
        return GitProbeResult(
            repo_id=repo_id(self.repo),
            upstream_sha=upstream_sha[:40],
            behind=behind,
            ahead=ahead,
            dirty=dirty,
            local_carried=local_carried,
            ff_eligible=behind > 0 and not dirty and not local_carried,
        )


def repo_id(repo: str | Path) -> str:
    safe, redacted = safe_durable_ref(str(Path(repo)), field="repo_path")
    if safe and not redacted:
        return safe
    return "repo:" + hashlib.sha256(str(Path(repo)).encode("utf-8")).hexdigest()[:16]


def _parse_counts(raw: str) -> tuple[int, int]:
    parts = raw.split()
    if len(parts) < 2:
        raise RuntimeError("unexpected rev-list count output")
    return int(parts[0]), int(parts[1])
