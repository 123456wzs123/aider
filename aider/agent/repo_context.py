"""Repository context: tracked files, repo map and git metadata."""

from __future__ import annotations

import subprocess
from pathlib import Path

from .tools.files import iter_repo_files


class ConsoleIO:
    """Minimal IO adapter for GitRepo / RepoMap."""

    encoding = "utf-8"

    def __init__(self, ui=None):
        self.ui = ui

    def read_text(self, fname, silent=False):
        try:
            return Path(fname).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def tool_output(self, *args, **kwargs):
        if self.ui is not None and getattr(self.ui, "verbose", False):
            self.ui.info(" ".join(str(arg) for arg in args))

    def tool_error(self, *args, **kwargs):
        if self.ui is not None:
            self.ui.error(" ".join(str(arg) for arg in args))

    def tool_warning(self, *args, **kwargs):
        if self.ui is not None:
            self.ui.error(" ".join(str(arg) for arg in args))


def current_branch(root) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=str(root),
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.decode("utf-8", errors="replace").strip()


class RepoContext:
    def __init__(self, root, model, ui=None, map_tokens=None):
        self.root = Path(root).resolve()
        self.model = model
        self.io = ConsoleIO(ui)
        self._tracked = None
        self._repo_map = None
        if map_tokens is None:
            try:
                map_tokens = model.get_repo_map_tokens()
            except Exception:
                map_tokens = 1024
        self.map_tokens = map_tokens

    def tracked_files(self) -> list[str]:
        if self._tracked is not None:
            return self._tracked

        files = None
        try:
            from aider.repo import GitRepo

            repo = GitRepo(self.io, None, str(self.root))
            files = repo.get_tracked_files()
        except Exception:
            files = None

        if files is None:
            files = list(iter_repo_files(self.root))
        self._tracked = sorted(str(name).replace("\\", "/") for name in files)
        return self._tracked

    def repo_map(self):
        if not self.map_tokens or self.map_tokens <= 0:
            return None
        files = self.tracked_files()
        if not files:
            return None

        try:
            from aider.repomap import RepoMap

            if self._repo_map is None:
                self._repo_map = RepoMap(
                    map_tokens=self.map_tokens,
                    root=str(self.root),
                    main_model=self.model,
                    io=self.io,
                    repo_content_prefix=None,
                    verbose=False,
                    max_context_window=(getattr(self.model, "info", None) or {}).get(
                        "max_input_tokens"
                    ),
                    map_mul_no_files=1,
                )
            abs_files = [str(self.root / name) for name in files]
            return self._repo_map.get_repo_map(set(), abs_files)
        except Exception:
            return None

    def close(self) -> None:
        cache = getattr(self._repo_map, "TAGS_CACHE", None)
        if cache is not None and hasattr(cache, "close"):
            try:
                cache.close()
            except Exception:
                pass
        self._repo_map = None
