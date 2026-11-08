"""CI step: validate every YAML file and print plain-language problems.

    python -m scripts.validate_config [config_dir]

Parser golden tests (each lab's sample reports vs expected_*.json) join this check in build step 1.
"""

import sys

from app.core.config import ConfigError, load_config


def main() -> int:
    config_dir = sys.argv[1] if len(sys.argv) > 1 else "config"
    try:
        cfg = load_config(config_dir)
    except ConfigError as exc:
        print(f"Config problems in {config_dir}/:")
        for problem in exc.problems:
            print(f"  - {problem}")
        return 1
    print(f"Config OK: {len(cfg.modules)} modules, {len(cfg.partners)} labs, {len(cfg.fields)} fields")
    return 0


if __name__ == "__main__":
    sys.exit(main())
