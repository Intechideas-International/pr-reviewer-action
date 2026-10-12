"""In-memory stand-ins for the GitHub REST API and the Anthropic Managed Agents API."""
import json
import os
from types import SimpleNamespace as NS

SHA = "a" * 40
OLD_SHA = "b" * 40
REPO = "acme/widgets"


def review_md(sha=SHA, quality=90):
    return (f"## 🤖 PR Review Sweep — {quality}/100 · Approval: ✅ Suggested\n\n"
            "Deep review (automated, sweep). **Verdict: 0 Required, 1 Suggested — fine.**\n\n"
            f"**Quality score: {quality}/100.** Good.\n\n### Architect assessment\n- **Blast radius**: small\n\n"
            "### Triage-signal vetting\n- none supplied\n\n### Findings\n1. **[Suggestion] [Suggested]** `a.py:1` x\n\n"
            f"<!-- agex-review: {sha} mode=deep score=none quality={quality} -->\n")


def findings(quality=90, **over):
    return json.dumps({"quality": quality, "required": 0, "suggested": 1, "findings": [], **over})


def pr(sha=SHA, fork=False):
    base = {"full_name": REPO}
    return {"number": 7, "title": "Add widget", "body": "desc", "user": {"login": "dev"}, "html_url": "u",
            "base": {"ref": "main", "sha": "c" * 40, "repo": base},
            "head": {"sha": sha, "repo": {"full_name": "evil/widgets" if fork else REPO}}}


def bot_review(sha, body_extra=""):
    return {"user": {"login": "github-actions[bot]"}, "state": "COMMENTED",
            "body": f"review {body_extra}\n<!-- agex-review: {sha} mode=deep score=none quality=90 -->"}


class FakeGitHub:
    def __init__(self, token=None, repo=REPO, pull=None, reviews=(), post_error=None):
        self.repo, self.pull, self._reviews = repo, pull or pr(), list(reviews)
        self.posted, self.post_error = [], post_error

    def pr(self, number):
        return self.pull

    def diff(self, pull):
        return b"diff --git a/a.py b/a.py\n"

    def ci(self, sha):
        return {"check_runs": [], "status": {"state": "success", "contexts": []}}

    def reviews(self, number):
        return list(self._reviews)

    def review_comments(self, number):
        return []

    def post_review(self, number, sha, body):
        if self.post_error:
            raise self.post_error
        self.posted.append((number, sha, body))
        self._reviews.append({"user": {"login": "bot"}, "state": "COMMENTED", "body": body})
        return {"html_url": "https://github.com/acme/widgets/pull/7#review"}


class FakeAPI:
    """Each created session plays the next script: {"stop": end_turn|budget_reached|...|None (never ends),
    "terminated": bool, "files": {name: text}, "cost": "97"}."""

    def __init__(self, scripts, agent=True, env=True):
        self.scripts, self.sessions, self.interrupts, self.uploads = list(scripts), {}, [], []
        self.created = []
        agents = [NS(name="pr-reviewer-acme", id="agent_1", version=3)] if agent else []
        envs = [NS(name="pr-reviewer-sandbox", id="env_1")] if env else []
        self.files = NS(upload=self._upload)
        self.beta = NS(
            agents=NS(list=lambda: agents),
            environments=NS(list=lambda: envs),
            sessions=NS(create=self._create, retrieve=self._retrieve,
                        events=NS(list=self._events, send=self._send)),
            files=NS(list=self._list_files, download=self._download),
        )

    def _upload(self, file, **kw):
        self.uploads.append(file[0])
        return NS(id=f"file_{len(self.uploads)}")

    def _create(self, **kw):
        sid = f"sesn_{len(self.created) + 1}"
        self.created.append(kw)
        self.sessions[sid] = self.scripts.pop(0)
        return NS(id=sid)

    def _events(self, session_id):
        s = self.sessions[session_id]
        if s.get("terminated"):
            return [NS(type="session.status_terminated")]
        if s.get("stop") is None:
            return [NS(type="session.status_running")]
        return [NS(type="session.status_running"),
                NS(type="session.status_idle", stop_reason=NS(type=s["stop"]))]

    def _send(self, session_id, events):
        self.interrupts.append(session_id)

    def _retrieve(self, session_id):
        cost = self.sessions[session_id].get("cost", "97")
        return NS(usage=NS(list_cost=NS(amount=cost), active_seconds=120.0))

    def _list_files(self, scope_id, betas):
        names = list(self.sessions[scope_id].get("files", {})) + ["diff.patch"]
        return [NS(id=f"{scope_id}/{n}", filename=n) for n in names]

    def _download(self, fid):
        sid, name = fid.split("/")
        text = self.sessions[sid]["files"][name]

        def write_to_file(path):
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
        return NS(write_to_file=write_to_file)


GOOD = {"stop": "end_turn", "files": {"review.md": review_md(), "findings.json": findings()}}


def read_outputs(path):
    out = {}
    if os.path.isfile(path):
        for line in open(path):
            k, _, v = line.strip().partition("=")
            out[k] = v
    return out
