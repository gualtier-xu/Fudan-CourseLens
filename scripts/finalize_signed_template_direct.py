"""Retired: signed-template-direct finalization is no longer available.

The direct mode was retired; no rollback metadata exists to finalize.
This script only reports the retirement and never mutates local or remote
state.
"""

from __future__ import annotations

import argparse
import json


RETIREMENT_REPORT = {
    "schema": "courselens.signed-template-direct-retired.v1",
    "status": "retired",
    "guidance": (
        "signed-template-direct mode was retired; run the client "
        "onboarding/bootstrap to use your personal Worker"
    ),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="", help="accepted for compatibility; unused")
    parser.add_argument("--proxy", default="", help="accepted for compatibility; unused")
    parser.parse_args(argv)
    print(json.dumps(RETIREMENT_REPORT, ensure_ascii=False, sort_keys=True, indent=2))
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
