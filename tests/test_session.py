import time

import pytest

from fakes import FakeAPI
from pr_reviewer import session


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(session.time, "sleep", lambda s: None)


def started(api):
    api.beta.sessions.create(agent={}, environment_id="env_1")
    return f"sesn_{len(api.created)}"


@pytest.mark.parametrize("stop,ok", [("end_turn", True), ("budget_reached", False), ("refusal", False),
                                     ("retries_exhausted", False)])
def test_stop_reasons(stop, ok):
    api = FakeAPI([{"stop": stop}])
    sid = started(api)
    status, reason = session.wait(api, sid, time.monotonic() + 60, poll_s=0)
    assert (status, reason) == ("session.status_idle", stop)
    assert session.succeeded(status, reason) is ok


def test_terminated_is_a_failure():
    api = FakeAPI([{"terminated": True}])
    status, reason = session.wait(api, started(api), time.monotonic() + 60, poll_s=0)
    assert status == "session.status_terminated" and not session.succeeded(status, reason)


def test_requires_action_keeps_waiting_until_deadline_then_interrupts():
    api = FakeAPI([{"stop": "requires_action"}])
    sid = started(api)
    assert session.wait(api, sid, time.monotonic() + 0.05, poll_s=0) == ("timeout", None)
    assert api.interrupts == [sid]


def test_timeout_interrupts_the_session():
    api = FakeAPI([{"stop": None}])
    sid = started(api)
    status, reason = session.wait(api, sid, time.monotonic() + 0.05, poll_s=0)
    assert status == "timeout" and not session.succeeded(status, reason)
    assert api.interrupts == [sid]


def test_download_only_fetches_the_outputs(tmp_path):
    api = FakeAPI([{"stop": "end_turn", "files": {"review.md": "r", "findings.json": "{}"}}])
    sid = started(api)
    assert session.download(api, sid, str(tmp_path)) == ["findings.json", "review.md"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["findings.json", "review.md"]


def test_start_sends_repo_resource_metadata_and_budget():
    api = FakeAPI([{"stop": "end_turn"}])
    agent, env = api.beta.agents.list()[0], api.beta.environments.list()[0]
    session.start(api, agent=agent, environment=env, client="acme", repo="acme/widgets", pr=7, sha="a" * 40,
                  pr_title="t", clone_token="tok", files={"pr.json": b"{}"}, budget_usd=15)
    kw = api.created[0]
    assert kw["metadata"] == {"client": "acme", "repo": "acme/widgets", "pr": "7", "sha": "a" * 40}
    assert kw["budget"] == {"type": "limit", "max_list_cost": {"amount": "1500", "currency": "USD"}}
    repo = kw["resources"][0]
    assert repo["type"] == "github_repository" and repo["checkout"] == {"type": "commit", "sha": "a" * 40}
    assert repo["mount_path"] == "/workspace/repo"
    assert kw["resources"][1]["mount_path"] == "/pr.json"
    assert kw["agent"] == {"type": "agent", "id": "agent_1", "version": 3}


def test_usage_reads_cents():
    api = FakeAPI([{"stop": "end_turn", "cost": "97"}])
    assert session.usage(api, started(api)) == (0.97, 120.0)
