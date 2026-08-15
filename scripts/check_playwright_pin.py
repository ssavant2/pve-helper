"""Assert that the Playwright runner image matches its npm client version.

The ``mcr.microsoft.com/playwright`` image ships only the browser build that its
own release expects, so bumping ``@playwright/test`` without bumping the image
leaves every browser test failing on a missing ``chrome-headless-shell`` — seven
minutes into the run, with an error that names neither file.  The two versions
live in different ecosystems and are updated by different dependency PRs, so
nothing but this check keeps them together.

Run: ``python scripts/check_playwright_pin.py``
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.tools.yml"
PACKAGE_FILE = REPO_ROOT / "e2e" / "package.json"

IMAGE_PATTERN = re.compile(r"^\s*image:\s*mcr\.microsoft\.com/playwright:v(?P<version>[\d.]+)-", re.MULTILINE)


def image_version() -> str:
    matches = IMAGE_PATTERN.findall(COMPOSE_FILE.read_text(encoding="utf-8"))
    if len(matches) != 1:
        raise SystemExit(
            f"Expected exactly one pinned Playwright image in {COMPOSE_FILE.name}, found {len(matches)}. "
            "Update this check alongside the compose file."
        )
    return matches[0]


def client_version() -> str:
    package = json.loads(PACKAGE_FILE.read_text(encoding="utf-8"))
    pinned = package.get("devDependencies", {}).get("@playwright/test")
    if not pinned:
        raise SystemExit(f"@playwright/test is not pinned in {PACKAGE_FILE}.")
    # The e2e client is pinned exactly so the image can match it; a range would
    # leave the pair unverifiable rather than merely mismatched.
    if not re.fullmatch(r"[\d.]+", pinned):
        raise SystemExit(f"@playwright/test must be pinned to an exact version, found {pinned!r}.")
    return pinned


def main() -> int:
    image = image_version()
    client = client_version()
    if image != client:
        print(
            f"Playwright version drift: {COMPOSE_FILE.name} runs image v{image} but "
            f"e2e/package.json pins @playwright/test {client}.\n"
            f"Move both together — set the image to mcr.microsoft.com/playwright:v{client}-noble "
            "with its current digest, or pin the client back to "
            f"{image}. Leaving them apart makes every browser test fail on a missing browser build.",
            file=sys.stderr,
        )
        return 1
    print(f"Playwright image and client agree on v{client}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
