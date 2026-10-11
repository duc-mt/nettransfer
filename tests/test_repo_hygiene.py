"""Guards for the things a GitHub project depends on: docs, links, workflows, release scripts."""

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def load(name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def run_script(name, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPTS / f"{name}.py"), *args], capture_output=True, text=True, cwd=ROOT
    )


def markdown_files():
    skip = {".git", "build", "dist", ".pytest_cache", ".ruff_cache", ".venv"}
    return [p for p in ROOT.rglob("*.md") if not (skip & set(p.relative_to(ROOT).parts))]


# ---------------------------------------------------------------- required files
@pytest.mark.parametrize(
    "name",
    [
        "README.md",
        "LICENSE",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        "docs/USER_GUIDE.md",
        "docs/ARCHITECTURE.md",
        "docs/PACKAGING.md",
        "docs/RELEASING.md",
        ".github/dependabot.yml",
        ".github/PULL_REQUEST_TEMPLATE.md",
        ".github/CODEOWNERS",
        ".github/workflows/ci.yml",
        ".github/workflows/build.yml",
        ".github/workflows/codeql.yml",
        ".gitignore",
        ".gitattributes",
        ".editorconfig",
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "requirements-test.txt",
    ],
)
def test_required_file_exists(name):
    assert (ROOT / name).is_file(), f"{name} is missing"


# ---------------------------------------------------------------- documentation
LINK = re.compile(r"\]\(([^)\s]+)\)")


def test_relative_markdown_links_resolve():
    broken = []
    for md in markdown_files():
        for target in LINK.findall(md.read_text(encoding="utf-8")):
            if re.match(r"^(https?:|mailto:|#)", target):
                continue
            path = target.split("#")[0]
            if path and not (md.parent / path).exists():
                broken.append(f"{md.relative_to(ROOT)} -> {target}")
    assert not broken, "broken relative links:\n" + "\n".join(broken)


def test_markdown_anchor_links_point_at_real_headings():
    def slug(heading):
        text = re.sub(r"[^\w\s-]", "", heading.strip().lower())
        return re.sub(r"\s", "-", text)

    bad = []
    for md in markdown_files():
        text = md.read_text(encoding="utf-8")
        headings = {slug(m.group(1)) for m in re.finditer(r"^#{1,6}\s+(.+)$", text, re.MULTILINE)}
        for target in LINK.findall(text):
            if target.startswith("#") and target[1:] not in headings:
                bad.append(f"{md.relative_to(ROOT)} -> {target}")
    assert not bad, "dangling in-page anchors:\n" + "\n".join(bad)


def test_readme_embeds_existing_screenshots():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    images = re.findall(r"\]\((docs/images/[^)]+)\)", readme)
    assert len(images) >= 3 and all((ROOT / i).stat().st_size > 5000 for i in images)


def test_repo_placeholder_is_consistent():
    """Verify no accidental stray generic placeholders remain."""
    offenders = []
    for path in [*markdown_files(), *(ROOT / ".github").rglob("*.yml"), ROOT / ".github" / "CODEOWNERS"]:
        text = path.read_text(encoding="utf-8")
        for pattern in (
            r"YOUR[_-]?(USER|NAME|ORG)",
            r"<owner>/<repo>",
            r"github\.com/(?!(duc-mt|OWNER))[\w.-]+/nettransfer",
        ):
            if re.search(pattern, text, re.IGNORECASE):
                offenders.append(f"{path.relative_to(ROOT)} matches {pattern}")
    assert not offenders, offenders


# ---------------------------------------------------------------- GitHub config
yaml = pytest.importorskip("yaml")


def load_yaml(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_least_privilege_and_has_timeouts(path):
    wf = load_yaml(path)
    assert "permissions" in wf, f"{path.name}: set top-level `permissions` (least privilege)"
    for name, job in wf["jobs"].items():
        if "uses" in job:  # a call to a reusable workflow: timeouts live in the called one
            continue
        assert "timeout-minutes" in job, f"{path.name}: job '{name}' has no timeout-minutes"


def test_build_workflow_covers_all_platforms_and_gates_the_release():
    wf = load_yaml(ROOT / ".github" / "workflows" / "build.yml")
    include = wf["jobs"]["package"]["strategy"]["matrix"]["include"]
    oses = {m["os"] for m in include}
    assert {"windows-latest", "ubuntu-22.04", "macos-latest"} <= oses and any("intel" in o for o in oses)
    release = wf["jobs"]["release"]
    assert release["needs"] == "package" and "refs/tags/v" in release["if"]
    assert release["permissions"]["contents"] == "write"
    assert wf["jobs"]["ci"]["uses"] == "./.github/workflows/ci.yml"
    assert "verify" in wf["jobs"]["package"]["needs"]


def test_ci_is_reusable_and_runs_the_expected_checks():
    ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    wf = load_yaml(ROOT / ".github" / "workflows" / "ci.yml")
    triggers = wf.get("on", wf.get(True))
    assert "workflow_call" in triggers and "pull_request" in triggers
    for needle in ("ruff check .", "ruff format --check .", "pytest", "scripts/check_version.py", "--selftest"):
        assert needle in ci_text, needle
    assert {"windows-latest", "macos-latest", "ubuntu-22.04"} <= {
        m["os"] for m in wf["jobs"]["test"]["strategy"]["matrix"]["include"]
    }


def test_dependabot_never_bumps_paramiko_to_5():
    """Pinned `<5` on purpose (paramiko 5 dropped SHA-1 kex / ssh-rsa); the config and the pin must stay in sync."""
    assert re.search(r"^paramiko>=4(\.\d+)*,<5", (ROOT / "requirements.txt").read_text(encoding="utf-8"), re.MULTILINE)
    config = load_yaml(ROOT / ".github" / "dependabot.yml")
    pip = next(u for u in config["updates"] if u["package-ecosystem"] == "pip")
    assert {"dependency-name": "paramiko", "update-types": ["version-update:semver-major"]} in pip["ignore"]
    assert {u["package-ecosystem"] for u in config["updates"]} == {"pip", "github-actions"}


def test_ruff_version_is_pinned_identically_everywhere():
    pinned = re.search(r"ruff==([\d.]+)", (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")).group(1)
    assert f"ruff=={pinned}" in (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"rev: v{pinned}" in (ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")


@pytest.mark.parametrize("path", sorted((ROOT / ".github" / "ISSUE_TEMPLATE").glob("*.yml")), ids=lambda p: p.name)
def test_issue_templates_are_valid_forms(path):
    data = load_yaml(path)
    if path.name == "config.yml":
        assert data["blank_issues_enabled"] is False and data["contact_links"]
        return
    assert data["name"] and data["description"] and data["body"]
    ids = [item["id"] for item in data["body"] if "id" in item]
    assert len(ids) == len(set(ids)), "duplicate field ids"
    for item in data["body"]:
        if item["type"] == "dropdown":
            assert item["attributes"]["options"]


def test_pyproject_is_valid_and_has_no_publishable_project_table():
    tomllib = pytest.importorskip("tomllib")  # Python 3.11+
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["ruff"]["line-length"] == 120
    assert "project" not in data and "build-system" not in data


# ---------------------------------------------------------------- release scripts
def test_current_version_and_changelog_are_consistent():
    result = run_script("check_version")
    assert result.returncode == 0, result.stderr


def test_check_version_rejects_wrong_tag_and_bad_changelog(tmp_path):
    check = load("check_version")
    version = check.read_version()
    assert check.check(f"v{version}") == []
    assert any("does not match" in p for p in check.check("v0.0.1"))
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text('__version__ = "1.2.3"\n')
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\n## [Unreleased]\n")
    assert any("no '## [1.2.3]'" in p for p in check.check(root=tmp_path))
    (tmp_path / "app" / "__init__.py").write_text('__version__ = "one.two"\n')
    assert any("MAJOR.MINOR.PATCH" in p for p in check.check(root=tmp_path))


def test_semver_accepts_prereleases():
    check = load("check_version")
    for ok in ("2.0.0", "2.1.0-rc.1", "10.20.30-beta"):
        assert check.SEMVER.match(ok)
    for bad in ("2.0", "v2.0.0", "2.0.0.1", ""):
        assert not check.SEMVER.match(bad)


def test_extract_changelog_returns_only_the_requested_section(tmp_path):
    extract = load("extract_changelog")
    log = tmp_path / "CHANGELOG.md"
    log.write_text(
        "# Changelog\n\n## [Unreleased]\n- next\n\n## [2.0.0] - 2026-01-01\n### Added\n- a\n\n"
        "## [1.0.0] - 2025-01-01\n- old\n\n[Unreleased]: https://x/compare\n"
    )
    assert extract.extract("2.0.0", log) == "### Added\n- a"
    assert extract.extract("1.0.0", log) == "- old"
    assert extract.extract("9.9.9", log) == ""


def test_real_changelog_section_is_publishable():
    result = run_script("extract_changelog", "v" + load("check_version").read_version())
    assert result.returncode == 0 and "###" in result.stdout and "## [" not in result.stdout
    assert run_script("extract_changelog", "0.0.0").returncode == 1


def test_set_repo_rewrites_placeholders_and_is_safe(tmp_path):
    if not (SCRIPTS / "set_repo.py").exists():
        pytest.skip("scripts/set_repo.py has been cleaned up after repo configuration")
    (tmp_path / "docs").mkdir()
    (tmp_path / "README.md").write_text("[CI](https://github.com/OWNER/REPO/actions) by @OWNER\n")
    (tmp_path / "docs" / "x.md").write_text("see github.com/OWNER/REPO\n")
    (tmp_path / "keep.png").write_bytes(b"OWNER/REPO")
    dry = run_script("set_repo", "me/nettransfer", "--dry-run", "--root", str(tmp_path))
    assert dry.returncode == 0 and "would update README.md" in dry.stdout
    assert "OWNER/REPO" in (tmp_path / "README.md").read_text()  # dry run changes nothing
    done = run_script("set_repo", "me/nettransfer", "--root", str(tmp_path))
    assert done.returncode == 0
    assert (tmp_path / "README.md").read_text() == "[CI](https://github.com/me/nettransfer/actions) by @me\n"
    assert (tmp_path / "keep.png").read_bytes() == b"OWNER/REPO"  # binaries are never touched
    assert run_script("set_repo", "OWNER/REPO").returncode == 2
    assert run_script("set_repo", "not a slug").returncode == 2
