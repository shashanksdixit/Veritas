"""Integration test — output-schema versioning (T022b, constitution Principle
VIII, FR-016).

A full pipeline run's ``Report.schema_version`` matches the version declared in
``data-model.md``; ``CHANGELOG.md`` (T004a) contains a corresponding entry for
that version. This is a tripwire that fails loudly if a schema field changes
without a matching version bump and changelog entry.
"""

import re
from pathlib import Path

from tests.conftest import default_fake_llm

from veritas.config.constants import SCHEMA_VERSION
from veritas.models.entities import ReviewScope
from veritas.review.graph import run_review


def test_schema_version_matches_data_model():
    repo_root = Path(__file__).parents[2]
    data_model = (repo_root / "specs" / "001-code-review" / "data-model.md").read_text(encoding="utf-8")
    match = re.search(r"schema_version:\s*str\s*=\s*[\"']([\d.]+)[\"']", data_model)
    assert match is not None
    declared = match.group(1)
    assert SCHEMA_VERSION == declared


def test_schema_version_recorded_in_changelog():
    changelog = Path(__file__).parents[2] / "CHANGELOG.md"
    assert changelog.is_file()
    text = changelog.read_text(encoding="utf-8")
    assert SCHEMA_VERSION in text


def test_schema_version_in_report_marker(sample_project, settings):
    outcome = run_review(settings, ReviewScope.PROJECT, str(sample_project), llm=default_fake_llm())
    md = Path(str(outcome.report_path)).read_text(encoding="utf-8")
    assert f"<!-- veritas-report-schema: {SCHEMA_VERSION} -->" in md