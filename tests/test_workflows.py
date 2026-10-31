"""The GitHub Actions workflows that reach AWS."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def test_no_job_names_a_github_environment():
    """The deploy roles in `infra` trust the `ref:refs/heads/main` OIDC subject. A job
    that names a GitHub environment gets `environment:<name>` as its subject instead,
    and the role refuses it (the first automatic staging deploy failed exactly so)."""
    offenders = {
        f"{path.name}:{i}"
        for path in WORKFLOWS.glob("*.yml")
        for i, line in enumerate(path.read_text().splitlines(), 1)
        if re.match(r"^\s+environment:\s*\S", line)
    }
    assert offenders == set(), sorted(offenders)


def test_run_job_staging_offers_known_jobs_and_never_production():
    """The dispatch that runs one job by name (import_legacy, status) reaches staging's
    jobs Lambda only, and offers only jobs the Lambda knows. Read as text: CI has no YAML
    parser, and the input block's shape is fixed."""
    from app.jobs import JOBS

    text = (WORKFLOWS / "run-job-staging.yml").read_text()
    found = re.search(r"^    inputs:\n      job:\n((?:        .*\n)+)", text, re.M)
    assert found, "no `job` input under workflow_dispatch.inputs"
    job = found.group(1)
    assert re.search(r"^        type: choice$", job, re.M)
    assert re.search(r"^        required: true$", job, re.M)
    options = re.findall(r"^          - (\S+)$", job, re.M)
    assert {"import_legacy", "status"} <= set(options) <= set(JOBS), options
    assert "STAGING_ROLE_ARN" in text and "PROD_" not in text


def test_run_job_staging_dispatches_the_job_asynchronously():
    """The import runs for up to 840 s. A synchronous invoke outlives the AWS CLI's 60 s read
    timeout, and the CLI's retry would start a second import beside the first: the workflow
    hands the job over (Event), never repeats the dispatch, and says where the result is."""
    text = (WORKFLOWS / "run-job-staging.yml").read_text()
    commands = [
        line for line in text.replace("\\\n", " ").splitlines() if "aws lambda invoke" in line
    ]
    assert commands and all("--invocation-type Event" in c for c in commands), commands
    assert "RequestResponse" not in text
    assert 'AWS_MAX_ATTEMPTS: "1"' in text
    assert "aws logs tail /aws/lambda/" in text
