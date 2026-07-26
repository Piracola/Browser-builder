"""Shared build engine for the Firefox-family portable browser packages.

Each browser repository checks this repository out as ``builder`` in GitHub
Actions and calls ``python builder/build.py``. Everything generic lives here:
version resolution, download + integrity verification, extraction, portable
injection, post-build verification and packaging.

The injection binaries themselves are vendored in ``bin/`` together with
``bin/manifest.json``, which records where they came from upstream and their
SHA-256 digests. Child repositories only carry configuration (``portable.ini``)
and their launcher.

Related repositories:
- Browser-builder: https://github.com/Piracola/Browser-builder
- Firefox-Libportable: https://github.com/Piracola/Firefox-Libportable
- Floorp_portable: https://github.com/Piracola/Floorp_portable
- Zen-Libportable: https://github.com/Piracola/Zen-Libportable
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import mmap
import os
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable, Optional

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

BUILDER_ROOT = Path(__file__).resolve().parent
DEFAULT_BIN_DIR = BUILDER_ROOT / "bin"
MANIFEST_NAME = "manifest.json"

# Files vendored in bin/ that participate in the injection step.
INJECTOR_SOURCE_NAME = "upcheck64.exe"
INJECTOR_RUN_NAME = "upcheck.exe"
PORTABLE_DLL_NAME = "portable64.dll"
EXAMPLE_INI_NAME = "portable(example).ini"
UPSTREAM_README_NAME = "README"
UPSTREAM_LICENSE_NAME = "LICENSE-libportable.txt"

# The module libportable patches: every Firefox-family process loads mozglue.dll
# at startup, so that is where the portable runtime gets hooked in.
INJECTION_CARRIER = "mozglue.dll"
MAX_PE_SCAN_BYTES = 64 * 1024 * 1024

# Never copied out of a child repo's libportable directory: injection tooling is
# owned by bin/, and shipping it inside the finished package only confuses users.
CONFIG_COPY_BLOCKLIST = {
    "upcheck.exe",
    "upcheck32.exe",
    "upcheck64.exe",
    "portable32.dll",
    "portable64.dll",
    "setdll.exe",
    "setdll32.exe",
    "setdll64.exe",
    "injectpe.bat",
}

BROWSERS: dict[str, dict] = {
    "firefox": {
        "display": "Firefox",
        "exe_name": "firefox.exe",
        "folder_name": "Firefox",
        "supports_lang": True,
    },
    "floorp": {
        "display": "Floorp",
        "exe_name": "floorp.exe",
        "folder_name": "Floorp",
        "supports_lang": False,
    },
    "zen": {
        "display": "Zen",
        "exe_name": "zen.exe",
        "folder_name": "Zen",
        "supports_lang": False,
    },
}

DEFAULT_FIREFOX_LANG = "en-US"
DEFAULT_PROFILE_PATH = "../Profiles"


class BuildError(RuntimeError):
    """Raised for every expected failure mode, so main() can report it cleanly."""


# --------------------------------------------------------------------------- #
# Generic helpers, also imported by tools/sync_libportable.py
# --------------------------------------------------------------------------- #

def is_retryable(exc: Exception) -> bool:
    """Retry timeouts, connection errors and server-side failures only.

    A 403 from a spent API rate limit or a 404 for a missing asset will not fix
    itself; retrying those just delays a clear error message.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is None:
        return True
    return status in (408, 429) or status >= 500


def retry(action: Callable[[], object], *, what: str, attempts: int = 3, delay: float = 5.0):
    """Run ``action``, retrying transient failures with a growing backoff."""
    for attempt in range(1, attempts + 1):
        try:
            return action()
        except Exception as exc:  # noqa: BLE001 - retried and finally re-raised
            if attempt == attempts or not is_retryable(exc):
                raise
            logger.warning("%s failed (attempt %d/%d): %s; retrying in %.0fs", what, attempt, attempts, exc, delay)
            time.sleep(delay)
            delay *= 2
    raise AssertionError("unreachable")


def github_headers(url: str) -> dict[str, str]:
    """Authenticate GitHub API calls when a token is available.

    Anonymous API access is limited to 60 requests/hour per IP, which CI runners
    share. Using the workflow token turns a flaky daily build into a reliable one.
    """
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"
    return headers


def request_json(url: str) -> dict:
    logger.info("Fetching metadata: %s", url)

    def _get() -> dict:
        with requests.get(url, timeout=(10, 60), headers=github_headers(url)) as response:
            response.raise_for_status()
            return response.json()

    return retry(_get, what=f"GET {url}")


def request_text(url: str) -> str:
    def _get() -> str:
        with requests.get(url, timeout=(10, 60), headers=github_headers(url)) as response:
            response.raise_for_status()
            return response.text

    return retry(_get, what=f"GET {url}")


def download_file(url: str, target: Path, *, attempts: int = 3) -> Path:
    """Download ``url`` to ``target`` through a .part file, with retries."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(target.suffix + ".part")

    def _download() -> None:
        if part.exists():
            part.unlink()
        with requests.get(url, stream=True, timeout=(10, 120)) as response:
            response.raise_for_status()
            with open(part, "wb") as handle:
                for chunk in response.iter_content(chunk_size=1 << 16):
                    if chunk:
                        handle.write(chunk)
        if not part.exists() or part.stat().st_size == 0:
            raise BuildError("downloaded file is empty")

    retry(_download, what=f"Download {url}", attempts=attempts)
    part.replace(target)
    return target


def file_digest(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_seven_zip(explicit: Optional[str] = None) -> str:
    """Locate a usable 7-Zip executable."""
    if explicit:
        if os.path.exists(explicit):
            return explicit
        raise BuildError(f"7-Zip not found at the given path: {explicit}")

    found = shutil.which("7z")
    if found:
        return found

    for candidate in (r"C:\Program Files\7-Zip\7z.exe", r"C:\Program Files (x86)\7-Zip\7z.exe"):
        if os.path.exists(candidate):
            return candidate

    raise BuildError("7-Zip not found. Install it, put 7z on PATH, or pass --seven-z-path.")


def run_seven_zip(seven_zip: str, arguments: list[str], *, cwd: Optional[Path] = None, what: str = "7-Zip") -> None:
    result = subprocess.run(
        [seven_zip, *arguments],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise BuildError(f"{what} failed (exit {result.returncode}): {detail}")


def pe_imported_dlls(path: Path) -> list[str]:
    """Return the DLL names in a PE file's import directory.

    Portable injection works by adding ``portable64.dll`` to the import table of
    the module that loads the browser runtime, so reading that table back is a
    direct, deterministic check that the injection actually landed.
    """
    with open(path, "rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            return _parse_pe_imports(data, path)


def _parse_pe_imports(data, path: Path) -> list[str]:
    if data[:2] != b"MZ":
        raise BuildError(f"Not a PE image: {path}")

    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset:pe_offset + 4] != b"PE\0\0":
        raise BuildError(f"Missing PE signature: {path}")

    coff = pe_offset + 4
    section_count = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    optional = coff + 20

    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x20B:      # PE32+
        data_directories = optional + 112
    elif magic == 0x10B:    # PE32
        data_directories = optional + 96
    else:
        raise BuildError(f"Unknown optional header magic 0x{magic:x}: {path}")

    import_rva, _import_size = struct.unpack_from("<II", data, data_directories + 8)
    if not import_rva:
        return []

    sections = []
    section_table = optional + optional_size
    for index in range(section_count):
        entry = section_table + index * 40
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from("<IIII", data, entry + 8)
        sections.append((virtual_address, max(virtual_size, raw_size), raw_pointer))

    def to_offset(rva: int) -> Optional[int]:
        for virtual_address, span, raw_pointer in sections:
            if virtual_address <= rva < virtual_address + span:
                offset = rva - virtual_address + raw_pointer
                if 0 <= offset < len(data):
                    return offset
        return None

    def read_cstring(offset: int) -> str:
        end = data.find(b"\0", offset, offset + 512)
        if end < 0:
            end = offset
        return data[offset:end].decode("ascii", "replace")

    names: list[str] = []
    cursor = to_offset(import_rva)
    while cursor is not None and cursor + 20 <= len(data):
        descriptor = data[cursor:cursor + 20]
        if descriptor == b"\0" * 20:
            break
        name_rva = struct.unpack_from("<I", descriptor, 12)[0]
        if name_rva:
            name_offset = to_offset(name_rva)
            if name_offset is not None:
                names.append(read_cstring(name_offset))
        cursor += 20
    return names


def load_manifest(bin_dir: Path) -> dict:
    manifest_path = bin_dir / MANIFEST_NAME
    if not manifest_path.exists():
        raise BuildError(f"Binary manifest not found: {manifest_path}")
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BuildError(f"Binary manifest is not valid JSON: {manifest_path}: {exc}") from exc


# --------------------------------------------------------------------------- #
# Builder
# --------------------------------------------------------------------------- #

class BrowserBuilder:
    def __init__(self, args: argparse.Namespace):
        self.browser_name: str = args.browser.lower()
        if self.browser_name not in BROWSERS:
            raise BuildError(f"Unsupported browser: {self.browser_name}")
        self.config = BROWSERS[self.browser_name]

        self.version: Optional[str] = args.version
        self.url: Optional[str] = args.url
        self.lang: str = args.lang
        self.auto_version: bool = args.auto_version
        self.check_only: bool = args.check_only
        self.keep_temp: bool = args.keep_temp
        self.allow_injection_warning: bool = args.allow_injection_warning
        self.skip_smoke_test: bool = args.skip_smoke_test
        self.smoke_test_timeout: int = args.smoke_test_timeout
        self.launcher_arg: Optional[str] = args.launcher

        # Only needed for a real build; --check-only just resolves a version.
        self.libportable_path: Optional[Path] = Path(args.libportable).resolve() if args.libportable else None
        if self.libportable_path and not self.libportable_path.is_dir():
            raise BuildError(f"Libportable config directory not found: {self.libportable_path}")
        if not self.check_only and self.libportable_path is None:
            raise BuildError("--libportable is required when building (it holds this browser's portable.ini).")

        self.bin_dir: Path = Path(args.bin_dir).resolve() if args.bin_dir else DEFAULT_BIN_DIR
        self.seven_zip: Optional[str] = None
        self.seven_z_path_arg: Optional[str] = args.seven_z_path

        self.workspace: Path = Path(args.workspace).resolve() if args.workspace else Path.cwd()
        self.temp_dir: Path = self.workspace / "temp_build"
        self.output_dir: Path = self.workspace / "output"
        self.installer_name: str = f"{self.browser_name}_installer.exe"

        # (algorithm, hex digest) published by upstream for the installer.
        self.expected_installer_digest: Optional[tuple[str, str]] = None
        self.libportable_version: str = "unknown"

        if self.lang != DEFAULT_FIREFOX_LANG and not self.config["supports_lang"]:
            logger.warning("--lang is only meaningful for Firefox; ignoring %r for %s", self.lang, self.browser_name)

    # ---------------------------------------------------------------- outputs

    def _write_github_output(self, **values: str) -> None:
        target = os.environ.get("GITHUB_OUTPUT")
        if not target:
            return
        with open(target, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")

    def _get_seven_zip(self) -> str:
        if self.seven_zip is None:
            self.seven_zip = resolve_seven_zip(self.seven_z_path_arg)
            logger.info("Using 7-Zip: %s", self.seven_zip)
        return self.seven_zip

    # --------------------------------------------------------------- resolve

    def _resolve_firefox(self) -> tuple[str, str]:
        data = request_json("https://product-details.mozilla.org/1.0/firefox_versions.json")
        version = data["LATEST_FIREFOX_VERSION"]
        return version, self._firefox_installer_url(version)

    def _firefox_installer_url(self, version: str) -> str:
        return (
            "https://download-installer.cdn.mozilla.net/pub/firefox/releases/"
            f"{version}/win64/{self.lang}/Firefox%20Setup%20{version}.exe"
        )

    def _firefox_expected_digest(self, version: str) -> tuple[str, str]:
        """Look the installer up in Mozilla's published SHA512SUMS for the release."""
        sums_url = f"https://download-installer.cdn.mozilla.net/pub/firefox/releases/{version}/SHA512SUMS"
        wanted = f"win64/{self.lang}/Firefox Setup {version}.exe"
        for line in request_text(sums_url).splitlines():
            digest, _, name = line.partition("  ")
            if name.strip() == wanted:
                return "sha512", digest.strip()
        raise BuildError(
            f"No SHA512 entry for {wanted!r} in {sums_url}. "
            f"Check that the language code {self.lang!r} is valid for this release."
        )

    def _resolve_github_release_asset(self, api_url: str, asset_name: str) -> tuple[str, str]:
        data = request_json(api_url)
        version = data["tag_name"]
        for asset in data.get("assets", []):
            if asset.get("name") == asset_name:
                digest = asset.get("digest") or ""
                if digest.startswith("sha256:"):
                    self.expected_installer_digest = ("sha256", digest.split(":", 1)[1])
                else:
                    logger.warning("Release asset %s has no published digest; skipping installer verification", asset_name)
                return version, asset["browser_download_url"]
        available = ", ".join(asset.get("name", "?") for asset in data.get("assets", []))
        raise BuildError(f"Release asset {asset_name!r} not found. Available assets: {available}")

    def resolve_version_and_url(self) -> None:
        if self.auto_version:
            if self.browser_name == "firefox":
                self.version, self.url = self._resolve_firefox()
            elif self.browser_name == "floorp":
                self.version, self.url = self._resolve_github_release_asset(
                    "https://api.github.com/repos/Floorp-Projects/Floorp/releases/latest",
                    "floorp-windows-x86_64.installer.exe",
                )
            elif self.browser_name == "zen":
                self.version, self.url = self._resolve_github_release_asset(
                    "https://api.github.com/repos/zen-browser/desktop/releases/latest",
                    "zen.installer.exe",
                )

        if not self.version or not self.url:
            raise BuildError("Version and URL must be provided, or use --auto-version.")

        logger.info("Resolved version: %s", self.version)
        logger.info("Resolved URL: %s", self.url)

        self._write_github_output(
            resolved_version=self.version,
            resolved_url=self.url,
            version=self.version,
            url=self.url,
            browser_display=self.config["display"],
        )

    # ------------------------------------------------------------- toolchain

    def verify_toolchain(self) -> None:
        """Check the vendored injection binaries against bin/manifest.json.

        This is what makes the shipped package auditable: the manifest records
        which upstream libportable release the binaries came from, and this check
        proves the files in the checkout are still exactly those bytes.
        """
        manifest = load_manifest(self.bin_dir)
        upstream = manifest.get("upstream", {})
        self.libportable_version = upstream.get("release_tag", "unknown")

        files = manifest.get("files", {})
        if not files:
            raise BuildError(f"Binary manifest lists no files: {self.bin_dir / MANIFEST_NAME}")

        for name, expected in sorted(files.items()):
            path = self.bin_dir / name
            if not path.exists():
                raise BuildError(f"Binary listed in the manifest is missing: {path}")

            actual_size = path.stat().st_size
            if "size" in expected and actual_size != expected["size"]:
                raise BuildError(f"{name}: size mismatch (expected {expected['size']}, got {actual_size})")

            actual = file_digest(path, "sha256")
            if actual != expected["sha256"]:
                raise BuildError(
                    f"{name}: SHA-256 mismatch.\n  expected {expected['sha256']}\n  actual   {actual}\n"
                    "The vendored binary does not match the manifest; refusing to inject it."
                )

        for required in (INJECTOR_SOURCE_NAME, PORTABLE_DLL_NAME):
            if not (self.bin_dir / required).exists():
                raise BuildError(f"Required injection binary missing from {self.bin_dir}: {required}")

        logger.info(
            "Verified %d injection binaries against the manifest (libportable %s).",
            len(files),
            self.libportable_version,
        )
        self._write_github_output(libportable_version=self.libportable_version)

    # -------------------------------------------------------------- download

    def download(self) -> Path:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        installer_path = self.temp_dir / self.installer_name

        if self.browser_name == "firefox" and self.version:
            self.expected_installer_digest = self._firefox_expected_digest(self.version)

        if installer_path.exists() and installer_path.stat().st_size > 0:
            logger.info("Installer already present, reusing: %s", installer_path)
        else:
            logger.info("Downloading %s ...", self.url)
            download_file(self.url, installer_path)
            logger.info("Downloaded %s (%d bytes)", installer_path, installer_path.stat().st_size)

        self._verify_installer(installer_path)
        return installer_path

    def _verify_installer(self, installer_path: Path) -> None:
        if not self.expected_installer_digest:
            logger.warning("No published digest available for this installer; integrity NOT verified.")
            return

        algorithm, expected = self.expected_installer_digest
        actual = file_digest(installer_path, algorithm)
        if actual != expected:
            installer_path.unlink(missing_ok=True)
            raise BuildError(
                f"Installer {algorithm.upper()} mismatch — refusing to build.\n"
                f"  expected {expected}\n  actual   {actual}\n"
                "The download does not match the digest published upstream."
            )
        logger.info("Installer %s verified against the upstream digest.", algorithm.upper())

    # --------------------------------------------------------------- extract

    def extract(self, installer_path: Path) -> Path:
        extract_dir = self.temp_dir / "extracted"
        shutil.rmtree(extract_dir, ignore_errors=True)
        extract_dir.mkdir(parents=True)

        logger.info("Extracting %s ...", installer_path)
        run_seven_zip(
            self._get_seven_zip(),
            ["x", str(installer_path), f"-o{extract_dir}", "-y"],
            what="Extraction",
        )

        self._remove_files(extract_dir, "setup.exe")

        shutil.rmtree(self.output_dir, ignore_errors=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)

        source_core = self._find_core_dir(extract_dir)
        target_core = self.output_dir / self.config["folder_name"]
        logger.info("Moving browser files: %s -> %s", source_core, target_core)
        shutil.move(str(source_core), str(target_core))

        if not (target_core / self.config["exe_name"]).exists():
            raise BuildError(f"{self.config['exe_name']} not found in the extracted files: {target_core}")
        return target_core

    def _remove_files(self, root: Path, filename: str) -> None:
        for path in root.rglob(filename):
            if path.is_file():
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("Could not remove %s: %s", path, exc)

    def _find_core_dir(self, extract_dir: Path) -> Path:
        exe_name = self.config["exe_name"].lower()
        if (extract_dir / "core" / self.config["exe_name"]).exists():
            return extract_dir / "core"
        for path in extract_dir.rglob("*"):
            if path.is_file() and path.name.lower() == exe_name:
                return path.parent
        raise BuildError(f"Could not locate {self.config['exe_name']} in the extracted installer.")

    # ---------------------------------------------------------------- inject

    def inject(self, core_dir: Path) -> None:
        logger.info("Injecting portable runtime ...")

        copied = 0
        for item in sorted(self.libportable_path.glob("*")):
            if item.is_file() and item.name.lower() not in CONFIG_COPY_BLOCKLIST:
                shutil.copy2(item, core_dir)
                copied += 1
        logger.info("Copied %d configuration file(s) from %s", copied, self.libportable_path)

        shutil.copy2(self.bin_dir / PORTABLE_DLL_NAME, core_dir / PORTABLE_DLL_NAME)
        # The package redistributes libportable's runtime, so its documentation
        # and licence travel with it.
        for name in (UPSTREAM_README_NAME, UPSTREAM_LICENSE_NAME):
            source = self.bin_dir / name
            if source.exists():
                shutil.copy2(source, core_dir / name)

        injector = core_dir / INJECTOR_RUN_NAME
        shutil.copy2(self.bin_dir / INJECTOR_SOURCE_NAME, injector)

        logger.info("Running injection: %s -dll", injector)
        result = subprocess.run(
            [str(injector), "-dll"],
            cwd=str(core_dir),
            capture_output=True,
            text=True,
            errors="replace",
        )
        if result.returncode != 0:
            message = (result.stderr or result.stdout or f"return code {result.returncode}").strip()
            if self.allow_injection_warning:
                logger.warning("Injection reported a problem (continuing because of --allow-injection-warning): %s", message)
            else:
                raise BuildError(f"Injection failed: {message}")
        else:
            logger.info("Injection reported success.")

        self._cleanup_injection_tools(core_dir)
        self._ensure_portable_ini(core_dir)

    def _cleanup_injection_tools(self, core_dir: Path) -> None:
        for pattern in ("upcheck*.exe", "setdll*.exe", "portable32.dll"):
            for path in core_dir.glob(pattern):
                try:
                    path.unlink()
                except OSError as exc:
                    logger.warning("Could not remove %s: %s", path, exc)

    def _ensure_portable_ini(self, core_dir: Path) -> None:
        target = core_dir / "portable.ini"
        if target.exists():
            return
        for fallback in (core_dir / EXAMPLE_INI_NAME, self.bin_dir / EXAMPLE_INI_NAME):
            if fallback.exists():
                logger.warning("No portable.ini supplied; falling back to %s", fallback)
                shutil.copy2(fallback, target)
                return
        raise BuildError("No portable.ini found in the libportable directory and no example available.")

    # ------------------------------------------------------------ verify PE

    def verify_injection(self, core_dir: Path) -> None:
        """Confirm the portable runtime is really wired into the browser.

        libportable patches ``mozglue.dll`` — the module every Firefox-family
        process loads at startup — rather than the launcher executable itself,
        so that is the module whose import table we read back.
        """
        if not (core_dir / PORTABLE_DLL_NAME).exists():
            raise BuildError(f"{PORTABLE_DLL_NAME} is missing from {core_dir}")

        wanted = PORTABLE_DLL_NAME.lower()
        carriers: list[str] = []
        checked: set[Path] = set()

        def imports_runtime(path: Path) -> bool:
            if path in checked or not path.is_file():
                return False
            checked.add(path)
            try:
                return wanted in {name.lower() for name in pe_imported_dlls(path)}
            except (BuildError, OSError, ValueError):
                return False

        for candidate in (core_dir / INJECTION_CARRIER, core_dir / self.config["exe_name"]):
            if imports_runtime(candidate):
                carriers.append(candidate.name)

        if not carriers:
            for candidate in sorted(core_dir.glob("*")):
                if candidate.suffix.lower() in (".exe", ".dll") and candidate.stat().st_size <= MAX_PE_SCAN_BYTES:
                    if imports_runtime(candidate):
                        carriers.append(candidate.name)

        if not carriers:
            raise BuildError(
                f"No module in {core_dir.name} imports {PORTABLE_DLL_NAME}; the package would not be portable.\n"
                f"Expected {INJECTION_CARRIER} to be patched by the injector."
            )
        logger.info("Verified: %s import(s) %s.", ", ".join(carriers), PORTABLE_DLL_NAME)

    # ----------------------------------------------------------- smoke test

    def _portable_data_path(self, core_dir: Path) -> Path:
        """Where portable.ini says the profile should live, relative to the browser dir."""
        configured = DEFAULT_PROFILE_PATH
        ini_path = core_dir / "portable.ini"
        if ini_path.exists():
            for raw in ini_path.read_text(encoding="utf-8", errors="replace").splitlines():
                line = raw.strip()
                if line.startswith(";") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                if key.strip().lower() == "portabledatapath" and value.strip():
                    configured = value.strip()
                    break
        return (core_dir / configured).resolve()

    def smoke_test(self) -> None:
        """Launch the built browser once and prove it stores its profile locally.

        A zero exit code from the injector only means the import table was
        patched. This runs the real thing: if the portable runtime is not
        working, the profile lands in %APPDATA% and the expected local profile
        directory stays empty.
        """
        if self.skip_smoke_test:
            logger.warning("Smoke test skipped (--skip-smoke-test); portability was NOT verified at runtime.")
            return

        stage = self.temp_dir / "smoketest"
        shutil.rmtree(stage, ignore_errors=True)
        shutil.copytree(self.output_dir, stage)

        core_dir = stage / self.config["folder_name"]
        exe_path = core_dir / self.config["exe_name"]
        profile_dir = self._portable_data_path(core_dir)

        appdata = Path(os.environ.get("APPDATA", "")) if os.environ.get("APPDATA") else None
        before = {entry.name for entry in appdata.iterdir()} if appdata and appdata.is_dir() else set()

        logger.info("Smoke test: launching %s headless (expecting a profile at %s) ...", exe_path.name, profile_dir)
        process = subprocess.Popen(
            [str(exe_path), "-headless", "-no-remote", "about:blank"],
            cwd=str(core_dir),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + self.smoke_test_timeout
            created = False
            while time.monotonic() < deadline:
                if profile_dir.is_dir() and any(p.is_file() for p in profile_dir.rglob("*")):
                    created = True
                    break
                if process.poll() is not None and profile_dir.is_dir():
                    created = any(p.is_file() for p in profile_dir.rglob("*"))
                    break
                time.sleep(1)
        finally:
            self._terminate_tree(process)

        after = {entry.name for entry in appdata.iterdir()} if appdata and appdata.is_dir() else set()
        appeared = sorted(after - before)

        if not created:
            detail = f" New %APPDATA% entries during the run: {', '.join(appeared)}." if appeared else ""
            raise BuildError(
                f"Smoke test failed: no profile appeared at {profile_dir} within {self.smoke_test_timeout}s.\n"
                f"The portable runtime is not redirecting user data, so this package is not portable.{detail}"
            )

        if appeared:
            logger.warning("Smoke test: new %%APPDATA%% entries appeared during the run: %s", ", ".join(appeared))
        logger.info("Smoke test passed: profile data was written to %s", profile_dir)
        shutil.rmtree(stage, ignore_errors=True)

    def _terminate_tree(self, process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(process.pid)],
            capture_output=True,
            text=True,
            errors="replace",
        )
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()

    # --------------------------------------------------------------- package

    def generate_launcher(self) -> None:
        if self.launcher_arg and Path(self.launcher_arg).exists():
            logger.info("Using launcher from the browser repository: %s", self.launcher_arg)
            shutil.copy2(self.launcher_arg, self.output_dir)
            return

        logger.info("No launcher supplied; generating a default 开始.bat")
        folder = self.config["folder_name"]
        display = self.config["display"]
        launcher = self.output_dir / "开始.bat"
        launcher.write_text(
            "@echo off\r\n"
            "chcp 65001 >nul\r\n"
            "setlocal\r\n"
            "\r\n"
            f'set "target=%~dp0{folder}\\{self.config["exe_name"]}"\r\n'
            f'set "lnk=%~dp0{display}.lnk"\r\n'
            "\r\n"
            'if not exist "%target%" (\r\n'
            "    echo [错误] 未找到 %target%\r\n"
            "    pause & exit /b 1\r\n"
            ")\r\n"
            "\r\n"
            "powershell -NoP -EP Bypass -C \"$w=New-Object -ComObject WScript.Shell;"
            "$s=$w.CreateShortcut('%lnk%');$s.TargetPath='%target%';"
            f"$s.WorkingDirectory='%~dp0{folder}';$s.Description='{display} 便携版';$s.Save()\" 2>nul\r\n"
            "\r\n"
            "if %errorlevel% neq 0 (\r\n"
            "    echo [错误] 创建快捷方式失败\r\n"
            "    pause & exit /b 1\r\n"
            ")\r\n"
            "\r\n"
            "echo [成功] 快捷方式已创建: %lnk%\r\n",
            encoding="utf-8",
        )

    def create_archive(self) -> Path:
        archive_name = f"{self.config['display']}_{self.version}.7z"
        archive_path = self.workspace / archive_name
        archive_path.unlink(missing_ok=True)

        logger.info("Creating archive %s ...", archive_name)
        run_seven_zip(
            self._get_seven_zip(),
            ["a", str(archive_path), "*", "-mx9"],
            cwd=self.output_dir,
            what="Archiving",
        )

        digest = file_digest(archive_path, "sha256")
        checksum_name = f"{archive_name}.sha256"
        (self.workspace / checksum_name).write_text(f"{digest}  {archive_name}\n", encoding="utf-8")

        size_mb = archive_path.stat().st_size / (1024 * 1024)
        logger.info("Archive created: %s (%.1f MiB)", archive_path, size_mb)
        logger.info("SHA-256: %s", digest)

        self._write_github_output(
            artifact_path=archive_name,
            artifact_name=archive_name,
            artifact_sha256=digest,
            checksum_path=checksum_name,
            version=self.version,
        )
        return archive_path

    # ------------------------------------------------------------------- run

    def cleanup(self) -> None:
        if self.keep_temp:
            logger.info("Keeping temporary directory: %s", self.temp_dir)
            return
        if self.temp_dir.exists():
            try:
                shutil.rmtree(self.temp_dir)
            except OSError as exc:
                logger.warning("Could not clean up %s: %s", self.temp_dir, exc)

    def run(self) -> None:
        try:
            self.resolve_version_and_url()
            if self.check_only:
                logger.info("Check-only mode: skipping download and build.")
                return

            self.verify_toolchain()
            installer = self.download()
            core_dir = self.extract(installer)
            self.inject(core_dir)
            self.verify_injection(core_dir)
            self.generate_launcher()
            self.smoke_test()
            self.create_archive()
            logger.info("Build finished: %s %s (libportable %s)", self.config["display"], self.version, self.libportable_version)
        finally:
            self.cleanup()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build portable Firefox-family browser packages with libportable.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
Browser repositories that use this shared builder:
  Firefox-Libportable: https://github.com/Piracola/Firefox-Libportable
  Floorp_portable:     https://github.com/Piracola/Floorp_portable
  Zen-Libportable:     https://github.com/Piracola/Zen-Libportable

Typical usage:
  python builder/build.py --browser firefox --auto-version --libportable libportable --launcher 开始.bat
""",
    )
    parser.add_argument("--browser", required=True, choices=sorted(BROWSERS), help="Browser to build")
    parser.add_argument("--version", help="Browser version (omit with --auto-version)")
    parser.add_argument("--url", help="Installer download URL (omit with --auto-version)")
    parser.add_argument("--auto-version", action="store_true", help="Resolve the latest version and installer URL")
    parser.add_argument("--check-only", action="store_true", help="Only resolve version and URL, then exit")
    parser.add_argument("--lang", default=DEFAULT_FIREFOX_LANG, help=f"Firefox installer language, e.g. zh-CN (default: {DEFAULT_FIREFOX_LANG})")
    parser.add_argument("--libportable", help="Directory holding this browser's portable.ini (required unless --check-only)")
    parser.add_argument("--bin-dir", help=f"Directory holding the injection binaries (default: {DEFAULT_BIN_DIR})")
    parser.add_argument("--launcher", help="Launcher script to ship inside the package")
    parser.add_argument("--workspace", help="Build workspace (default: current directory)")
    parser.add_argument("--seven-z-path", help="Path to 7z.exe")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temp_build after the run")
    parser.add_argument("--skip-smoke-test", action="store_true", help="Skip the post-build portability check (debug only)")
    parser.add_argument("--smoke-test-timeout", type=int, default=120, help="Seconds to wait for the profile to appear (default: 120)")
    parser.add_argument(
        "--allow-injection-warning",
        action="store_true",
        help="Continue when the injector returns a non-zero code (debug only)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        BrowserBuilder(args).run()
    except BuildError as exc:
        logger.error("Build failed: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001 - top-level guard for unexpected failures
        logger.exception("Build failed with an unexpected error: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
