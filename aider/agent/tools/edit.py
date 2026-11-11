"""edit_file: SEARCH/REPLACE blocks applied through the fork's edit engine."""

from __future__ import annotations

from ..edit_engine import apply_edit_text
from . import ToolSpec


def edit_file(context, args) -> dict:
    edits = args.get("edits")
    if not edits or not str(edits).strip():
        return {"ok": False, "error": "edits is required"}
    result = apply_edit_text(str(edits), context.root, context.valid_fnames)
    return result.to_dict()


EDIT_TOOL = ToolSpec(
    name="edit_file",
    description=(
        "Apply one or more SEARCH/REPLACE blocks. Put the repository-relative file path "
        "on the line before <<<<<<< SEARCH. Each file is all-or-nothing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "edits": {
                "type": "string",
                "description": (
                    "Text containing blocks of the form:\n"
                    "path/to/file.py\n"
                    "<<<<<<< SEARCH\n"
                    "exact existing lines\n"
                    "=======\n"
                    "replacement lines\n"
                    ">>>>>>> REPLACE"
                ),
            }
        },
        "required": ["edits"],
    },
    handler=edit_file,
)
