"""Check a session's outputs before anything is posted: review.md follows the template and ends with the
marker for the head commit; findings.json is the expected shape."""
import json
import os
import re

SECTIONS = ("## 🤖 PR Review Sweep — ", "**Quality score:", "### Architect assessment",
            "### Triage-signal vetting", "### Findings")


def _is_int(v, lo=0, hi=None):
    return isinstance(v, int) and not isinstance(v, bool) and v >= lo and (hi is None or v <= hi)


def check_review(text, sha):
    errors, pos = [], 0
    for s in SECTIONS:
        i = text.find(s, pos)
        if i < 0:
            errors.append(f"review.md: missing or out of order: {s.strip()}")
        else:
            pos = i
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if not lines or not re.fullmatch(rf"<!-- agex-review: {re.escape(sha)} .*-->", lines[-1]):
        errors.append(f"review.md: last line is not the agex-review marker for {sha}")
    return errors


def check_findings(text):
    try:
        data = json.loads(text)
    except ValueError as e:
        return [f"findings.json: invalid JSON ({e})"]
    if not isinstance(data, dict):
        return ["findings.json: not an object"]
    errors = []
    if not _is_int(data.get("quality"), 0, 100):
        errors.append("findings.json: quality must be an int 0-100")
    for k in ("required", "suggested"):
        if not _is_int(data.get(k)):
            errors.append(f"findings.json: {k} must be a non-negative int")
    if not isinstance(data.get("findings"), list):
        errors.append("findings.json: findings must be a list")
    return errors


def validate(directory, sha):
    """Return the list of problems with the outputs in directory; empty means OK to post."""
    errors = []
    for name, check in (("review.md", lambda t: check_review(t, sha)), ("findings.json", check_findings)):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            errors.append(f"{name}: missing")
        else:
            errors += check(open(path, encoding="utf-8").read())
    return errors
