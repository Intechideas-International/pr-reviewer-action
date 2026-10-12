import pytest

from fakes import SHA, findings, review_md
from pr_reviewer.validate import validate


def write(tmp_path, review=None, found=None):
    if review is not None:
        (tmp_path / "review.md").write_text(review, encoding="utf-8")
    if found is not None:
        (tmp_path / "findings.json").write_text(found, encoding="utf-8")
    return str(tmp_path)


def test_valid_outputs_pass(tmp_path):
    assert validate(write(tmp_path, review_md(), findings()), SHA) == []


def test_missing_review_md(tmp_path):
    assert validate(write(tmp_path, found=findings()), SHA) == ["review.md: missing"]


def test_missing_findings_json(tmp_path):
    assert validate(write(tmp_path, review=review_md()), SHA) == ["findings.json: missing"]


def test_invalid_json(tmp_path):
    errors = validate(write(tmp_path, review_md(), "{not json"), SHA)
    assert len(errors) == 1 and errors[0].startswith("findings.json: invalid JSON")


@pytest.mark.parametrize("quality", [101, -1, "90", 90.0, True, None])
def test_quality_must_be_int_0_to_100(tmp_path, quality):
    assert validate(write(tmp_path, review_md(), findings(quality=quality)), SHA) == [
        "findings.json: quality must be an int 0-100"]


@pytest.mark.parametrize("over,error", [
    ({"required": -1}, "findings.json: required must be a non-negative int"),
    ({"suggested": "1"}, "findings.json: suggested must be a non-negative int"),
    ({"findings": {}}, "findings.json: findings must be a list"),
])
def test_counts_and_findings_shape(tmp_path, over, error):
    assert validate(write(tmp_path, review_md(), findings(**over)), SHA) == [error]


def test_findings_not_an_object(tmp_path):
    assert validate(write(tmp_path, review_md(), "[]"), SHA) == ["findings.json: not an object"]


def test_marker_for_another_sha_fails(tmp_path):
    errors = validate(write(tmp_path, review_md(sha="b" * 40), findings()), SHA)
    assert errors == [f"review.md: last line is not the agex-review marker for {SHA}"]


def test_marker_must_be_last_line(tmp_path):
    errors = validate(write(tmp_path, review_md() + "\ntrailing text\n", findings()), SHA)
    assert errors == [f"review.md: last line is not the agex-review marker for {SHA}"]


def test_missing_section(tmp_path):
    body = review_md().replace("### Architect assessment\n", "")
    assert validate(write(tmp_path, body, findings()), SHA) == ["review.md: missing or out of order: ### Architect assessment"]


def test_rereview_rejects_suggestions_and_nits(tmp_path):
    rule = "findings.json: a re-review has no Suggestions or Nits (they go under Noted)"
    carried = findings(suggested=0, findings=[{"severity": "Suggestion", "tag": "Suggested"}])
    assert validate(write(tmp_path, review_md(), carried), SHA, rereview=True) == [rule]
    assert validate(write(tmp_path, review_md(), findings()), SHA, rereview=True) == [rule]  # suggested=1
    clean = findings(suggested=0, findings=[{"severity": "Important", "tag": "Required"}], required=1)
    assert validate(write(tmp_path, review_md(), clean), SHA, rereview=True) == []
    assert validate(write(tmp_path, review_md(), carried), SHA) == []  # first reviews may suggest
