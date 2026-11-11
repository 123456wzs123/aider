"""Approval policy for tools with side effects."""

from __future__ import annotations


class PermissionPolicy:
    """Reads and edits are allowed; shell commands need approval.

    ``approver`` is a callable ``(command) -> "y" | "n" | "a"`` injected by
    tests; otherwise the UI's ``confirm_command`` is used.  ``a`` remembers the
    first two tokens of the command for the rest of the session.
    """

    def __init__(self, yes=False, dry_run=False, allow_edits=True, approver=None):
        self.yes = yes
        self.dry_run = dry_run
        self.allow_edits = allow_edits
        self.approver = approver
        self._always: set[str] = set()

    @staticmethod
    def command_prefix(command: str) -> str:
        parts = command.strip().split()
        if not parts:
            return ""
        return " ".join(parts[:2]) if len(parts) > 1 else parts[0]

    def allow_command(self, command: str, ui=None) -> bool:
        if self.yes:
            return True
        prefix = self.command_prefix(command)
        if prefix in self._always:
            return True

        if self.approver is not None:
            answer = str(self.approver(command) or "n")
        elif ui is not None and hasattr(ui, "confirm_command"):
            answer = str(ui.confirm_command(command) or "n")
        else:
            answer = "n"

        answer = answer.strip().lower()[:1]
        if answer == "a":
            self._always.add(prefix)
            return True
        return answer == "y"
