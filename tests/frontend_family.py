"""Family-source reader for split frontend modules (ARCH-DEBT-1).

The architecture-debt batch splits oversized modules (settings.js 等) into
``frontend/modules/<stem>/*.js`` submodules behind a re-export facade. Source
content pins must search the *family* (facade + its submodules concatenated),
not the facade file alone: a pure move keeps the family union text
behavior-invariant, so pins over the union keep their exact strength while
surviving the new file layout. With no subdirectory present the union
degenerates to the facade text, so these readers are safe to adopt before any
split lands.
"""

import re
from pathlib import Path

FRONTEND_MODULES = Path(__file__).resolve().parents[1] / "frontend" / "modules"


def family_text(stem: str) -> str:
    """Return facade ``<stem>.js`` plus every ``<stem>/*.js`` submodule text."""
    facade = (FRONTEND_MODULES / f"{stem}.js").read_text(encoding="utf-8")
    family_dir = FRONTEND_MODULES / stem
    if not family_dir.is_dir():
        return facade
    parts = [facade]
    for entry in sorted(family_dir.glob("*.js")):
        parts.append(entry.read_text(encoding="utf-8"))
    return "\n".join(parts)


def family_entity(stem: str, name: str) -> str:
    """Extract one top-level entity's source from the ``stem`` family union.

    Used by tests that eval real source slices (update guidance/diagnostics
    evals): after the ARCH-DEBT-1 split a family's internals may live in
    different files, so name-anchored region slices can no longer assume the
    whole evaluated set sits contiguously in one file. The slice runs from the
    entity's top-level declaration to the next top-level declaration in the
    family union; leading comments of the next entity are not included.
    """
    text = family_text(stem)
    decl = re.compile(r"^(export\s+)?(async\s+)?(function\s*\*?|const|let|class)\s+[A-Za-z_$]", re.M)
    starts = [m.start() for m in decl.finditer(text)]
    for i, start in enumerate(starts):
        end_of_line = text.find("\n", start)
        head = text[start:(end_of_line + 1) if end_of_line != -1 else len(text)]
        if re.search(r"(function\s*\*?|const|let|class)\s+%s\b" % re.escape(name), head):
            end = starts[i + 1] if i + 1 < len(starts) else len(text)
            return text[start:end].rstrip("\n") + "\n"
    raise AssertionError(f"top-level entity {name!r} not found in {stem} family")
