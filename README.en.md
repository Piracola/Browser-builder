# Browser-builder

[![Validate Builder](https://img.shields.io/github/actions/workflow/status/Piracola/Browser-builder/validate.yml?style=flat-square&label=Validate)](https://github.com/Piracola/Browser-builder/actions/workflows/validate.yml)
[![libportable](https://img.shields.io/badge/libportable-v9.0.9-blue?style=flat-square)](https://github.com/adonais/libportable/releases/tag/v9.0.9)
[![License](https://img.shields.io/github/license/Piracola/Browser-builder?style=flat-square&label=License)](LICENSE)

[简体中文](README.md) | **English**

The shared builder for the Firefox-family portable browsers.

This repository is not aimed at end users. It is the build engine shared by three browser repositories, turning official upstream installers into ready-to-run portable packages. If you just want a browser, download a finished build from one of the repositories below.

| Browser repository | Description |
|------|------|
| [Firefox-Libportable](https://github.com/Piracola/Firefox-Libportable) | Portable Firefox |
| [Floorp_portable](https://github.com/Piracola/Floorp_portable) | Portable Floorp |
| [Zen-Libportable](https://github.com/Piracola/Zen-Libportable) | Portable Zen |

## How the repositories are split

The project has two layers with a very clear boundary between them:

| Layer | Contents |
|------|------|
| **Browser-builder** (this repository) | The `build.py` build script, the injection binaries in `bin/`, the reusable workflow, and the upstream sync tooling |
| **Browser repositories** | Just three things: `libportable/portable.ini`, `开始.bat`, and a dozen-line workflow that calls the reusable one |

In other words, **every executable and every piece of build logic lives here**, and the browser repositories hold nothing but configuration. Change the build process in one place and all three browsers pick it up at once.

## Build pipeline

The full `build.py` pipeline. Steps marked with ✅ are verification gates that fail the build outright:

| Step | Description |
|------|------|
| 1. Resolve the version | Firefox is resolved from Mozilla's `firefox_versions.json`; Floorp and Zen from the GitHub Releases API |
| 2. ✅ Verify the injection binaries | Every file's SHA-256 is checked against `bin/manifest.json`; any mismatch aborts the build |
| 3. Download the installer | Retries on failure, and writes to a temporary file before renaming so a truncated download is never mistaken for a complete one |
| 4. ✅ Verify the installer | Firefox is compared against the official `SHA512SUMS`; Floorp and Zen against the asset digest published by GitHub |
| 5. Extract | Unpack the installer with 7-Zip and locate the browser's core directory |
| 6. Inject the portable runtime | Copy in `portable.ini` and `portable64.dll`, then run `upcheck.exe -dll` |
| 7. ✅ Verify the injection | Read the PE import table of `mozglue.dll` and confirm that `portable64.dll` really was written into it |
| 8. Copy the launcher | Use the `开始.bat` shipped by the browser repository |
| 9. ✅ Runtime portability check | Launch the browser once headless and confirm that user data lands in the portable directory rather than `%APPDATA%` |
| 10. Package | Produce `<Browser>_<version>.7z` along with a matching `.sha256` checksum file |

Step 9 is the most important part of the whole pipeline. When the injection tool returns 0, that only means "the import table was rewritten successfully" — it does not mean the browser is actually portable. This step copies the finished package, really starts it, and then checks whether the directory named by `PortableDataPath` in the configuration file actually received any user data. **A browser that was not injected never creates that directory**, so this check genuinely catches the worst class of problem for users: injection that silently stops working, for example after a major browser release breaks the hook.

## Verification and provenance

How much you can trust a build comes down to being able to explain, at every stage, where the bytes came from:

| Stage | How it is guaranteed |
|------|----------|
| Official installer | Compared byte for byte against the hash published upstream; any mismatch aborts immediately |
| Injection binaries | `bin/manifest.json` records the upstream repository, the release tag, the archive SHA-256, and a per-file SHA-256, all re-verified on every build |
| Builder version | Every build records the commit SHA of the builder it actually checked out in the release notes, so any release can be reproduced exactly |
| Build artifact | The SHA-256 is included in the release notes, and a `.sha256` file is uploaded alongside the package |

### The bin/ directory

`bin/` holds the binaries used for injection, all taken from official releases of [adonais/libportable](https://github.com/adonais/libportable):

```text
bin/
├── manifest.json              Provenance and hash manifest
├── upcheck64.exe              The injector
├── portable64.dll             Portable runtime (shipped with the package)
├── portable(example).ini      Upstream config template (fallback when a browser repo ships none)
├── README                     Upstream documentation (shipped with the package)
└── LICENSE-libportable.txt    Upstream license (shipped with the package)
```

### Syncing with upstream

The repository runs a weekly [Check libportable Upstream](.github/workflows/check-libportable.yml) workflow that opens an issue whenever a new upstream release appears. It **never replaces the binaries automatically** — the injection components decide whether a build is portable at all, so a human has to confirm the change.

```powershell
# Check whether a new version is available
python tools/sync_libportable.py --check

# Sync to a specific version (re-downloads, verifies, and updates bin/ and manifest.json)
python tools/sync_libportable.py --tag v9.0.9
```

After a sync, trigger a build manually in any of the browser repositories and confirm that the portability checks still pass before letting the scheduled builds resume.

## Local usage

### Prerequisites

| Dependency | Notes |
|------|------|
| Python 3.10+ | Matches the version used in GitHub Actions |
| requests | `pip install -r requirements.txt` |
| 7-Zip | `7z` must be callable, or point at it with `--seven-z-path` |

### Common commands

```powershell
# Resolve the latest version and download URL only, without downloading or packaging (the fastest smoke check)
python build.py --browser firefox --auto-version --check-only

# Full build
python build.py --browser firefox --auto-version `
  --libportable ..\Firefox-Libportable\libportable `
  --launcher ..\Firefox-Libportable\开始.bat

# Build the Simplified Chinese Firefox
python build.py --browser firefox --auto-version --lang zh-CN `
  --libportable ..\Firefox-Libportable\libportable
```

### Options

| Option | Purpose |
|------|------|
| `--browser` | Which browser: `firefox`, `floorp`, or `zen` |
| `--auto-version` | Resolve the latest version and installer URL automatically |
| `--check-only` | Resolve the version only; skip downloading and packaging |
| `--version` / `--url` | Specify the version number and installer URL manually |
| `--lang` | Firefox installer language, `en-US` by default; `zh-CN` and others are available |
| `--libportable` | The directory in the browser repository that holds `portable.ini` (may be omitted with `--check-only`) |
| `--launcher` | The launcher script to include in the package |
| `--bin-dir` | Directory holding the injection binaries; defaults to this repository's `bin/` |
| `--workspace` | Build working directory; defaults to the current directory |
| `--seven-z-path` | Path to `7z.exe` |
| `--keep-temp` | Keep `temp_build/` around for troubleshooting |
| `--smoke-test-timeout` | Seconds to wait during the runtime portability check; 120 by default |
| `--skip-smoke-test` | Skip the runtime portability check (debugging only) |
| `--allow-injection-warning` | Continue even when injection returns a non-zero exit code (debugging only) |

The last two options bypass the safety net and should never be used for a real build.

## Reusable workflow

All three browser repositories share [`.github/workflows/build-portable.yml`](.github/workflows/build-portable.yml), so each repository's own workflow needs only a handful of lines:

```yaml
jobs:
  build:
    permissions:
      contents: write
      actions: write
    uses: Piracola/Browser-builder/.github/workflows/build-portable.yml@main
    with:
      browser: firefox
      lang: en-US
      release-notes-url: 'https://www.mozilla.org/en-US/firefox/{version}/releasenotes/'
```

| Input | Default | Description |
|----------|--------|------|
| `browser` | required | `firefox` / `floorp` / `zen` |
| `lang` | `en-US` | Firefox installer language |
| `release-notes-url` | empty | Upstream release notes URL; `{version}` is replaced with the version number |
| `builder-ref` | `main` | Branch or tag to check the builder out at |
| `python-version` | `3.10` | Python version used for the build |
| `keep-workflow-runs` | `10` | Number of historical workflow runs to keep |

For a fully reproducible build, point both the `uses:` reference and `builder-ref` at the same tag:

```yaml
    uses: Piracola/Browser-builder/.github/workflows/build-portable.yml@v1
    with:
      browser: firefox
      builder-ref: v1
```

(A reusable workflow cannot see the commit it is running from — `github.job_workflow_sha` is empty in this context — so the reference has to be passed in explicitly.)

The workflow resolves the version, compares it against `last_version.txt` (only scheduled runs skip when the version is unchanged), builds, publishes a release, and writes the version record back.

## Build artifacts

```text
Firefox_153.0.7z          The finished archive
Firefox_153.0.7z.sha256   Checksum file
```

Inside GitHub Actions the following output variables are also written:

| Output | Meaning |
|----------|------|
| `resolved_version` / `resolved_url` | The resolved version and installer URL |
| `browser_display` | Display name of the browser, used in the release title |
| `artifact_name` / `artifact_path` | File name of the finished archive |
| `artifact_sha256` | SHA-256 of the finished archive |
| `checksum_path` | File name of the `.sha256` checksum file |
| `libportable_version` | The libportable version used for this build |

## Adding support for another browser

Nearly any Firefox-family browser can be plugged in directly. Add it in two places in `build.py`:

1. The `BROWSERS` dictionary: the executable name, the output folder name, and the display name.
2. `resolve_version_and_url`: how the version and installer URL are resolved.

Then create a new browser repository containing `libportable/portable.ini`, `开始.bat`, and the configuration that calls the reusable workflow.

**Do not push browser-specific configuration into this repository** — keeping that boundary is exactly what makes this layering pay off over time.

## FAQ

### 7-Zip cannot be found

Make sure `7z` runs directly, or point at it explicitly: `--seven-z-path "C:\Program Files\7-Zip\7z.exe"`.

### The injection binary hashes do not match

The files in `bin/` no longer agree with `manifest.json`. If you updated the upstream version on purpose, regenerate the manifest with `tools/sync_libportable.py --tag <version>` rather than editing the hashes by hand.

### The runtime portability check fails

The finished package started but produced no user data in the portable directory, which means the package is not portable and must not be released. The usual cause is that a major browser release has broken libportable's hooks, in which case you have to wait for an upstream update. Add `--keep-temp` to preserve the intermediate files while investigating.

### GitHub API rate limit

Anonymous calls to the GitHub API get only 60 requests per hour. The CI workflows already pass `GITHUB_TOKEN` automatically; if you hit the rate limit while debugging locally, just set the environment variable:

```powershell
$env:GITHUB_TOKEN = (gh auth token)
```

## License

This repository is released under the MIT License; see [LICENSE](LICENSE) for details.

The binaries in `bin/` come from [libportable](https://github.com/adonais/libportable) and are covered by its own license, included as [bin/LICENSE-libportable.txt](bin/LICENSE-libportable.txt).
