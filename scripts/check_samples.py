"""Run every lab's parser against tests/samples/<lab>/ and print plain-language problems.

Usage: python -m scripts.check_samples        (exit code 1 when anything differs; used in CI)
"""

import json
import sys
from pathlib import Path

from app.core.config import ConfigError, load_config
from app.domain.parsing import parse_report

SAMPLES = Path("tests/samples")


def main() -> int:
    try:
        cfg = load_config("config")
    except ConfigError as exc:
        print("\n".join(exc.problems))
        return 1
    problems: list[str] = []
    for key, partner in cfg.partners.items():
        if partner.parser != "generic":
            continue
        files = sorted([*(SAMPLES / key).glob("*.pdf"), *(SAMPLES / key).glob("*.docx")])
        if not files:
            problems.append(f"partners/{key}.yaml: no sample reports found in tests/samples/{key}/")
        for path in files:
            res = parse_report(path.read_bytes(), path.name, partner, cfg)
            for label in res.missing_labels:
                problems.append(f"partners/{key}.yaml: label '{label}' not found in sample {path.name}")
            expected_file = path.parent / f"expected_{path.stem}.json"
            if not expected_file.exists():
                problems.append(f"{expected_file}: expected results file is missing")
                continue
            expected = json.loads(expected_file.read_text(encoding="utf-8"))
            got = {k: v for k, v in res.fields.items() if v not in (None, "", [])}
            for field in sorted(set(expected) | set(got)):
                if expected.get(field) != got.get(field):
                    problems.append(
                        f"partners/{key}.yaml: '{field}' in {path.name} is {got.get(field)!r}, expected {expected.get(field)!r}"
                    )
    if problems:
        print("Sample report check failed:\n  - " + "\n  - ".join(problems))
        return 1
    print("All sample reports parse as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())