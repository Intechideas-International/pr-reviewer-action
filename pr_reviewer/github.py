"""GitHub REST calls the runner needs: read a PR and its context, find earlier bot reviews, post a review.

A bot review is a PR review posted by a bot account (user.type "Bot", e.g. github-actions[bot]) whose
body ends with the marker line `<!-- agex-review: <full head_sha> ... -->`. Only the last non-empty line
counts, as in validate, so a marker quoted earlier in the body (from the diff, say) is ignored; and a
person cannot suppress the reviewer by posting a review that carries the marker.

The reviewer's +1, in repos that allow it, is a separate APPROVED review ending with
`<!-- agex-approval: <full head_sha> -->`. It may come from a person's or machine user's token, so it
is matched by marker and state, not by account type.
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request

MARKER = re.compile(r"<!-- agex-review: ([0-9a-f]{40})\b.*-->")
APPROVAL = re.compile(r"<!-- agex-approval: ([0-9a-f]{40}) -->")


class GitHub:
    def __init__(self, token, repo, api_url=None):
        self.token, self.repo = token, repo
        self.api_url = (api_url or os.environ.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")

    def request(self, method, path, body=None, accept="application/vnd.github+json"):
        """Return (response bytes, Link header). path is relative to the repo, or a full URL."""
        url = path if path.startswith("http") else f"{self.api_url}/repos/{self.repo}/{path}"
        req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {self.token}", "Accept": accept,
                                              "X-GitHub-Api-Version": "2022-11-28"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read(), r.headers.get("Link", "")

    def get(self, path):
        return json.loads(self.request("GET", path)[0])

    def get_all(self, path, key=None):
        """Every item of a paginated list; key names the list inside a wrapper object (check-runs)."""
        items, url = [], f"{path}{'&' if '?' in path else '?'}per_page=100"
        while url:
            data, link = self.request("GET", url)
            page = json.loads(data)
            items += page[key] if key else page
            url = next((p.split(";")[0].strip(" <>") for p in link.split(",") if 'rel="next"' in p), None)
        return items

    def pr(self, number):
        return self.get(f"pulls/{number}")

    def diff(self, pr):
        return self.request("GET", f"compare/{pr['base']['sha']}...{pr['head']['sha']}",
                            accept="application/vnd.github.diff")[0]

    def ci(self, sha):
        """CI state of the head commit. Needs checks: read and statuses: read; reports what it could not read."""
        out = {}
        try:
            out["check_runs"] = [{"name": c["name"], "status": c["status"], "conclusion": c["conclusion"]}
                                 for c in self.get_all(f"commits/{sha}/check-runs", key="check_runs")]
        except urllib.error.HTTPError as e:
            out["check_runs"] = f"unavailable (HTTP {e.code})"
        try:
            s = self.get(f"commits/{sha}/status")
            out["status"] = {"state": s["state"],
                             "contexts": [{"context": c["context"], "state": c["state"]} for c in s["statuses"]]}
        except urllib.error.HTTPError as e:
            out["status"] = f"unavailable (HTTP {e.code})"
        return out

    def reviews(self, number):
        return self.get_all(f"pulls/{number}/reviews")

    def review_comments(self, number):
        return self.get_all(f"pulls/{number}/comments")

    def post_review(self, number, sha, body):
        return json.loads(self.request("POST", f"pulls/{number}/reviews",
                                       {"commit_id": sha, "body": body, "event": "COMMENT"})[0])

    def approve(self, number, sha, body):
        return json.loads(self.request("POST", f"pulls/{number}/reviews",
                                       {"commit_id": sha, "body": body, "event": "APPROVE"})[0])

    def dismisses_stale_approvals(self, branch):
        """True when a ruleset on branch dismisses approvals once new commits are pushed. Classic branch
        protection needs admin to read, so it is not seen here and counts as no."""
        rules = self.get_all(f"rules/branches/{urllib.parse.quote(branch, safe='')}")
        return any(r.get("type") == "pull_request" and (r.get("parameters") or {}).get("dismiss_stale_reviews_on_push")
                   for r in rules)


def _last_line(body, pattern):
    lines = (body or "").strip().splitlines()
    m = pattern.fullmatch(lines[-1].strip()) if lines else None
    return m.group(1) if m else None


def marker_sha(body):
    """Head SHA of the marker on the body's last non-empty line, or None."""
    return _last_line(body, MARKER)


def approval_sha(review):
    """Head SHA the reviewer's +1 was given for; None for any other review."""
    return _last_line(review.get("body"), APPROVAL) if review.get("state") == "APPROVED" else None


def bot_sha(review):
    """Head SHA a bot review was written for; None for reviews by people or without the marker."""
    return marker_sha(review.get("body")) if (review.get("user") or {}).get("type") == "Bot" else None


def is_fork(pr):
    head = pr["head"].get("repo")
    return not head or head["full_name"] != pr["base"]["repo"]["full_name"]


def already_reviewed(reviews, sha):
    return any(bot_sha(r) == sha for r in reviews)


def previous_review(reviews, sha):
    """The latest bot review of an earlier head commit, as {head_sha, body}, or None."""
    prior = [r for r in reviews if bot_sha(r) not in (None, sha)]
    if not prior:
        return None
    last = prior[-1]
    return {"head_sha": bot_sha(last), "body": last["body"]}


def human_reviews(reviews, comments=()):
    """Reviews that are neither bot reviews nor the reviewer's +1, then inline review comments, for the
    reviewer to dedupe against."""
    out = [{"user": r["user"]["login"], "state": r["state"], "body": r.get("body") or "",
            "submitted_at": r.get("submitted_at")}
           for r in reviews if bot_sha(r) is None and approval_sha(r) is None and r.get("user")]
    return out + [{"user": c["user"]["login"], "path": c.get("path"), "line": c.get("line"), "body": c["body"]}
                  for c in comments if c.get("user")]
