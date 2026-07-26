"""Vendor the libportable injection binaries from upstream, with provenance.

``bin/`` holds the binaries that get injected into every browser we package, and
``bin/manifest.json`` records exactly where they came from: the upstream release
tag, the SHA-256 of the release archive, and the SHA-256 of every extracted file.
``build.py`` re-checks those digests on every build and refuses to inject
anything that does not match.

Usage::

    python tools/sync_libportable.py --check     # is there a newer release?
    python tools/sync_libportable.py             # vendor the latest release
    python tools/sync_libportable.py --tag v9.0.9

Upstream: https://github.com/adonais/libportable
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from build import (  # noqa: E402  - path is set up above
    BuildError,
    download_file,
    file_digest,
    load_manifest,
    request_json,
    resolve_seven_zip,
    run_seven_zip,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BUILDER_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = BUILDER_ROOT / "bin"

UPSTREAM_REPO = "adonais/libportable"
UPSTREAM_URL = f"https://github.com/{UPSTREAM_REPO}"
RELEASE_ASSET = "portable_bin.7z"

# Only the pieces we actually use are vendored. The 32-bit binaries are omitted
# because every package we build is win64.
VENDORED_FILES = (
    "upcheck64.exe",
    "portable64.dll",
    "portable(example).ini",
    "README",
)


def fetch_release(tag: str | None) -> dict:
    if tag:
        return request_json(f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/tags/{tag}")
    return request_json(f"https://api.github.com/repos/{UPSTREAM_REPO}/releases/latest")


def find_asset(release: dict) -> dict:
    for asset in release.get("assets", []):
        if asset.get("name") == RELEASE_ASSET:
            return asset
    available = ", ".join(asset.get("name", "?") for asset in release.get("assets", []))
    raise BuildError(f"Release {release.get('tag_name')} has no {RELEASE_ASSET!r}. Available: {available}")


def current_manifest() -> dict:
    try:
        return load_manifest(BIN_DIR)
    except BuildError:
        return {}


def write_github_output(**values: str) -> None:
    target = os.environ.get("GITHUB_OUTPUT")
    if not target:
        return
    with open(target, "a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={value}\n")


def check(tag: str | None) -> int:
    release = fetch_release(tag)
    asset = find_asset(release)
    latest_tag = release["tag_name"]
    latest_digest = (asset.get("digest") or "").removeprefix("sha256:")

    manifest = current_manifest()
    upstream = manifest.get("upstream", {})
    current_tag = upstream.get("release_tag", "(none)")
    current_digest = upstream.get("asset_sha256", "")

    up_to_date = latest_tag == current_tag and (not latest_digest or latest_digest == current_digest)

    logger.info("Vendored libportable: %s", current_tag)
    logger.info("Latest upstream:      %s (published %s)", latest_tag, release.get("published_at", "?"))

    write_github_output(
        update_available="false" if up_to_date else "true",
        current_tag=current_tag,
        latest_tag=latest_tag,
        release_url=release.get("html_url", UPSTREAM_URL),
        published_at=release.get("published_at", ""),
    )

    if up_to_date:
        logger.info("Up to date.")
        return 0

    logger.warning("A newer libportable release is available: %s -> %s", current_tag, latest_tag)
    logger.warning("Run: python tools/sync_libportable.py --tag %s", latest_tag)
    return 0


def sync(tag: str | None, seven_z_path: str | None, workspace: Path) -> int:
    release = fetch_release(tag)
    asset = find_asset(release)
    release_tag = release["tag_name"]
    expected_digest = (asset.get("digest") or "").removeprefix("sha256:")

    workspace.mkdir(parents=True, exist_ok=True)
    archive = workspace / RELEASE_ASSET
    logger.info("Downloading %s %s ...", UPSTREAM_REPO, release_tag)
    download_file(asset["browser_download_url"], archive)

    actual_digest = file_digest(archive, "sha256")
    if expected_digest and actual_digest != expected_digest:
        raise BuildError(
            f"{RELEASE_ASSET} SHA-256 mismatch.\n  expected {expected_digest}\n  actual   {actual_digest}"
        )
    if not expected_digest:
        logger.warning("Upstream published no digest for %s; recording the downloaded digest instead.", RELEASE_ASSET)

    extract_dir = workspace / "extracted"
    shutil.rmtree(extract_dir, ignore_errors=True)
    run_seven_zip(
        resolve_seven_zip(seven_z_path),
        ["x", str(archive), f"-o{extract_dir}", "-y"],
        what="Extraction",
    )

    BIN_DIR.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict] = {}
    for name in VENDORED_FILES:
        matches = [p for p in extract_dir.rglob(name) if p.is_file()]
        if not matches:
            raise BuildError(f"{name!r} not found in {RELEASE_ASSET} ({release_tag})")
        source = matches[0]
        target = BIN_DIR / name
        shutil.copy2(source, target)
        files[name] = {"sha256": file_digest(target, "sha256"), "size": target.stat().st_size}
        logger.info("Vendored %-22s %9d bytes  %s", name, files[name]["size"], files[name]["sha256"])

    manifest = {
        "_comment": (
            "Provenance for the injection binaries in this directory. "
            "build.py verifies every digest before injecting, and refuses to build on a mismatch. "
            "Regenerate with: python tools/sync_libportable.py --tag <tag>"
        ),
        "upstream": {
            "project": "libportable",
            "repository": UPSTREAM_URL,
            "release_tag": release_tag,
            "release_url": release.get("html_url", UPSTREAM_URL),
            "published_at": release.get("published_at", ""),
            "asset": RELEASE_ASSET,
            "asset_sha256": actual_digest,
            "synced_at": time.strftime("%Y-%m-%d"),
        },
        "files": files,
    }
    manifest_path = BIN_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    logger.info("Wrote %s", manifest_path)
    logger.info("Vendored libportable %s. Review the diff, then commit bin/.", release_tag)
    write_github_output(synced_tag=release_tag)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="Only report whether a newer release exists")
    parser.add_argument("--tag", help="Vendor a specific release tag instead of the latest")
    parser.add_argument("--seven-z-path", help="Path to 7z.exe")
    parser.add_argument(
        "--workspace",
        default=str(BUILDER_ROOT / "temp_libportable"),
        help="Scratch directory for the download",
    )
    args = parser.parse_args()

    try:
        if args.check:
            return check(args.tag)
        return sync(args.tag, args.seven_z_path, Path(args.workspace).resolve())
    except BuildError as exc:
        logger.error("%s", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
