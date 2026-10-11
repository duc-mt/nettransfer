# Releasing (maintainers)

Releases are cut by pushing a version tag. CI then tests, builds the executables on Windows, macOS (arm64 + Intel) and
Linux, and publishes a GitHub Release with the CHANGELOG section as its notes, a combined `SHA256SUMS.txt` and
build-provenance attestations. Nothing is built or uploaded by hand.

## Checklist

1. **Decide the version** ([SemVer](https://semver.org)): `MAJOR.MINOR.PATCH`, or `X.Y.Z-rc.1` for a pre-release.
2. **Update `app/__init__.py`** (`__version__`).
3. **Update `CHANGELOG.md`**: rename `## [Unreleased]` content into a new `## [X.Y.Z] - YYYY-MM-DD` section, leave an empty
   `## [Unreleased]` above it, and fix the compare links at the bottom.
4. **Check consistency locally**: `make check-release TAG=vX.Y.Z` (version format, CHANGELOG section, tag equals version).
5. **Merge to `main`** through a PR; wait for CI to be green (lint, tests on Linux/Windows/macOS, launch self-test, CodeQL).
6. **Tag and push**:

   ```bash
   git switch main && git pull
   git tag -a vX.Y.Z -m "Network File Transfer X.Y.Z"
   git push origin vX.Y.Z
   ```

7. **Watch the *Build and release* workflow.** Jobs: `verify` (tag = version = CHANGELOG), `ci` (full suite), `package` x4
   (each runs the CLI and GUI smoke tests *inside the freshly built binary*), `release`.
8. **Check the release page**: all archives present, notes correct, `SHA256SUMS.txt` matches, pre-release flag set for `-rc`/`-beta` tags.
9. **Smoke-test a download** on at least one real machine per OS (start it, serve a file, push a file).

Dry-run the packaging without releasing: run the workflow manually (*Actions > Build and release > Run workflow*) or open a
PR that touches `packaging/`, `app/`, `run.py` or `requirements*.txt` (the package job runs for those; it does not publish).

## Signing and notarization (optional, off until secrets exist)

Unsigned binaries work but trigger Windows SmartScreen and macOS Gatekeeper warnings. The workflow signs automatically when
these repository secrets exist, and skips the steps otherwise:

| Secret | Used for |
|---|---|
| `WINDOWS_CERT_PFX` | base64 of the code-signing certificate (`.pfx`) |
| `WINDOWS_CERT_PASSWORD` | its password |
| `MACOS_CERTIFICATE_P12` | base64 of the *Developer ID Application* certificate (`.p12`) |
| `MACOS_CERTIFICATE_PASSWORD` | its password |
| `MACOS_SIGNING_IDENTITY` | e.g. `Developer ID Application: Your Name (TEAMID)` |
| `APPLE_ID`, `APPLE_TEAM_ID`, `APPLE_APP_PASSWORD` | notarization (an app-specific password) |

Encode a certificate with `base64 -w0 cert.p12` (Linux) or `base64 -i cert.p12 | tr -d '\n'` (macOS).

> **Status:** these steps follow Microsoft's and Apple's documented flows but could not be exercised without certificates.
> Expect to debug the first signed run (keychain import, `signtool` path, notarization wait). Unsigned releases are unaffected.

## Pre-releases

Tag `vX.Y.Z-rc.1` (and set `__version__ = "X.Y.Z-rc.1"`). The release is marked *pre-release*; the Windows/macOS numeric
version fields use the base `X.Y.Z`. Create the CHANGELOG section as `## [X.Y.Z-rc.1]`.

## If something goes wrong

* **A job fails before publishing:** fix on `main`, delete the tag (`git push --delete origin vX.Y.Z; git tag -d vX.Y.Z`),
  re-tag the fixed commit.
* **A bad release is already public:** mark it as a pre-release or delete it in the GitHub UI, delete the tag, ship a
  patch version. Do not move a published tag: checksums and attestations refer to the original build.
* **A runner label disappears** (GitHub retires images, e.g. `macos-13` in Dec 2025): update the matrix in
  `.github/workflows/build.yml` and `ci.yml`; Dependabot proposes action updates but not runner labels.

## Repository setup (once)

1. Verify repository remote points to `https://github.com/duc-mt/nettransfer.git` (badges, links, and templates configured).
2. **Settings > Branches**: protect `main`; require pull requests and the **CI passed** status check; optionally require
   code-owner review (see `.github/CODEOWNERS`) and linear history.
3. **Settings > Code security**: enable the dependency graph, Dependabot alerts and updates, secret scanning, **private
   vulnerability reporting** (referenced by `SECURITY.md`) and CodeQL default/advanced setup (the workflow is included).
4. **Settings > Actions > General**: workflow permissions *Read repository contents* (jobs request more only where needed).
5. Create the labels used by release notes and templates: `bug`, `enhancement`, `feature`, `docs`, `breaking`, `device-compat`,
   `dependencies`, `ci`, `skip-changelog`.
6. Enable **Discussions** (linked from the issue chooser).
