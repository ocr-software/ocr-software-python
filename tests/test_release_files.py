"""The files that go to PyPI agree with each other."""
import re
from pathlib import Path

import ocr_software

ROOT = Path(__file__).resolve().parent.parent


def test_version_is_the_same_everywhere():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^version = "%s"$' % re.escape(ocr_software.__version__), pyproject, re.M)


def test_readme_file_exists_and_no_runtime_dependencies():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert (ROOT / "INSTALL.txt").is_file() and 'file = "INSTALL.txt"' in pyproject
    assert "dependencies = []" in pyproject  # standard library only
    # 13.11.2026: as text/plain PyPI showed one fixed-width block whose long lines ran off the page
    assert 'content-type = "text/markdown"' in pyproject


def test_readme_that_pypi_shows_has_no_draft_note():
    """Review 13.11.2026: INSTALL.txt is the PyPI page; "Raw draft ... Not on PyPI yet" stood on it."""
    text = (ROOT / "INSTALL.txt").read_text(encoding="utf-8")
    assert "Raw draft" not in text and "Not on PyPI" not in text


def test_publish_workflow_is_trusted_publishing_with_tests_and_tag_check_before_upload():
    """Review 13.11.2026: no token, OIDC, environment pypi, release trigger, tag = version, tests before the upload step."""
    wf = (ROOT / ".github" / "workflows" / "publish.yml").read_text(encoding="utf-8")
    for needed in ("types: [published]", "id-token: write", "environment: pypi", "GITHUB_REF_NAME"):
        assert needed in wf
    assert "password" not in wf and "secrets." not in wf
    assert wf.index("GITHUB_REF_NAME") < wf.index("pytest") < wf.index("twine check") < wf.index("gh-action-pypi-publish")
