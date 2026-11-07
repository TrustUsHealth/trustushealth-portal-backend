"""Write (or check) openapi.json, the contract the frontend generates its typed client from.

python -m scripts.export_openapi          # rewrite openapi.json
python -m scripts.export_openapi --check  # exit 1 if openapi.json is stale (used in CI)
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017")  # never connected to
os.environ.setdefault("JWT_SECRET", "x" * 32)
os.environ.setdefault("ENVIRONMENT", "test")

from app.main import create_app  # noqa: E402

TARGET = Path("openapi.json")


def main() -> int:
    spec = json.dumps(create_app().openapi(), indent=2, sort_keys=True) + "\n"
    if "--check" in sys.argv:
        if not TARGET.exists() or TARGET.read_text() != spec:
            print("openapi.json is out of date. Run: python -m scripts.export_openapi")
            return 1
        print("openapi.json is up to date")
        return 0
    TARGET.write_text(spec)
    print(f"wrote {TARGET} ({len(spec)} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
