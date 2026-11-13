"""Remove <cac:Contact> blocks from the saved TED fixtures, in place. Offline, one-off.

Usage:
    python scripts/redact_ted_fixtures.py

Uses the same redact_contacts() as scripts/fetch_ted_fixtures.py (through redact_notice(), which
adds a guard for leftover contact markers). Steps:
  1. Redact every tests/fixtures/ted/*.xml in memory and check that it still parses.
     If any file fails, nothing is written.
  2. Write the redacted files as bytes and update manifest.json (sha256, redacted,
     contact_blocks_removed).
  3. Re-open every written file with xml.etree.ElementTree.parse.

Prints file names and counts only, never contact data. Safe to run again: it removes 0 blocks.
"""

from __future__ import annotations

import hashlib
import json
import sys
import xml.etree.ElementTree as ET

from fetch_ted_fixtures import OUT_DIR, REPO_ROOT, Stop, redact_notice


def main() -> int:
    paths = sorted(OUT_DIR.glob("*.xml"))
    if not paths:
        print(f"no XML files in {OUT_DIR.relative_to(REPO_ROOT)}")
        return 1

    # Phase 1: everything in memory first, so a failure leaves all files untouched.
    results: dict[str, tuple[bytes, int]] = {}
    for path in paths:
        try:
            results[path.name] = redact_notice(path.stem, path.read_bytes())
        except Stop as exc:
            print(f"{exc}; nothing written")
            return 1

    # Phase 2: write the files and bring the manifest in line.
    for path in paths:
        path.write_bytes(results[path.name][0])

    manifest_path = OUT_DIR / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest["notices"]:
            data, blocks = results[entry["file"]]
            entry["sha256"] = hashlib.sha256(data).hexdigest()
            entry["redacted"] = True
            # Cumulative, so a second run (0 blocks) does not erase the first run's count.
            entry["contact_blocks_removed"] = entry.get("contact_blocks_removed", 0) + blocks
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    # Phase 3: every written file must parse.
    failed = []
    for path in paths:
        try:
            ET.parse(path)
        except ET.ParseError as exc:
            failed.append((path.name, str(exc)))

    print(f"{'file':<18}{'blocks removed':>15}")
    for path in paths:
        print(f"{path.name:<18}{results[path.name][1]:>15}")
    print(f"{'total':<18}{sum(blocks for _, blocks in results.values()):>15}")
    print(f"manifest updated: {manifest_path.exists()}")
    if failed:
        for name, error in failed:
            print(f"PARSE FAILED {name}: {error}")
        return 1
    print(f"all {len(paths)} files parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
