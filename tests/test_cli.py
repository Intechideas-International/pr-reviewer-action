import json
import urllib.error

import anthropic
import pytest

from fakes import GOOD, OLD_SHA, REPO, SHA, FakeAPI, FakeGitHub, bot_review, findings, pr, read_outputs, review_md
from pr_reviewer import cli, github, session


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("GITHUB_TOKEN", "t")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "output"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
    monkeypatch.setattr(session.time, "sleep", lambda s: None)
    return tmp_path


def wire(monkeypatch, gh, api):
    monkeypatch.setattr(github, "GitHub", lambda token, repo: gh)
    monkeypatch.setattr(anthropic, "Anthropic", lambda: api)


def review(env, *extra):
    return cli.main(["review", "--client", "acme", "--repo", REPO, "--pr", "7", "--out", str(env / "out"),
                     "--session-file", str(env / "session-id"), "--poll-seconds", "0", *extra])


def test_review_success_leaves_outputs_and_session_id(env, monkeypatch):
    api, gh = FakeAPI([GOOD]), FakeGitHub()
    wire(monkeypatch, gh, api)
    assert review(env) == cli.EXIT_OK
    out = read_outputs(env / "output")
    assert out["result"] == "reviewed" and out["session-id"] == "sesn_1" and out["head-sha"] == SHA
    assert (env / "session-id").read_text() == "sesn_1"
    assert (env / "out" / "review.md").read_text(encoding="utf-8") == review_md()
    record = json.loads((env / "out" / "session.json").read_text())
    assert record["result"] == "reviewed" and record["attempts"][0]["cost_usd"] == 0.97
    assert "sesn_1" in (env / "summary.md").read_text() and "$0.97" in (env / "summary.md").read_text()
    assert gh.posted == []  # the review job never posts
    assert sorted(api.uploads) == ["context.json", "diff.patch", "pr.json"]


REREVIEW_GOOD = {"stop": "end_turn", "files": {"review.md": review_md(), "findings.json": findings(suggested=0)}}


def test_context_carries_previous_review_and_human_reviews(env, monkeypatch):
    human = {"user": {"login": "alice"}, "state": "COMMENTED", "body": "please rename"}
    api, gh = FakeAPI([REREVIEW_GOOD]), FakeGitHub(reviews=[bot_review(OLD_SHA, "earlier"), human])
    wire(monkeypatch, gh, api)
    captured = {}
    real_start = session.start
    monkeypatch.setattr(session, "start", lambda api_, **kw: captured.update(kw["files"]) or real_start(api_, **kw))
    assert review(env) == cli.EXIT_OK
    context = json.loads(captured["context.json"])
    assert context["previous_review"]["head_sha"] == OLD_SHA
    assert [r["user"] for r in context["human_reviews"]] == ["alice"]


def test_invalid_output_is_retried_once(env, monkeypatch):
    bad = {"stop": "end_turn", "files": {"review.md": review_md(), "findings.json": findings(quality=200)}}
    api = FakeAPI([bad, GOOD])
    wire(monkeypatch, FakeGitHub(), api)
    assert review(env) == cli.EXIT_OK
    assert len(api.created) == 2
    assert read_outputs(env / "output")["session-id"] == "sesn_2"


def test_two_failures_fail_the_run_and_leave_nothing_to_post(env, monkeypatch):
    missing = {"stop": "end_turn", "files": {"findings.json": findings()}}
    api = FakeAPI([missing, missing])
    wire(monkeypatch, FakeGitHub(), api)
    assert review(env) == cli.EXIT_FAILED
    assert read_outputs(env / "output")["result"] == "failed"
    assert not (env / "out" / "review.md").exists()
    assert cli.main(["post", "--repo", REPO, "--pr", "7", "--dir", str(env / "out")]) == cli.EXIT_OK


def test_budget_reached_is_a_failure(env, monkeypatch):
    spent = {"stop": "budget_reached", "files": {"review.md": review_md(), "findings.json": findings()}}
    api = FakeAPI([spent, spent])
    wire(monkeypatch, FakeGitHub(), api)
    assert review(env) == cli.EXIT_FAILED
    record = json.loads((env / "out" / "session.json").read_text())
    assert [a["errors"] for a in record["attempts"]] == [["session ended session.status_idle/budget_reached"]] * 2


def test_rereview_carrying_suggestions_is_retried_and_post_rechecks(env, monkeypatch):
    api = FakeAPI([GOOD, REREVIEW_GOOD])  # GOOD has a Suggestion: not allowed on a re-review
    wire(monkeypatch, FakeGitHub(reviews=[bot_review(OLD_SHA)]), api)
    assert review(env) == cli.EXIT_OK and len(api.created) == 2
    assert json.loads((env / "out" / "session.json").read_text())["rereview"] is True
    (env / "out" / "findings.json").write_text(findings(), encoding="utf-8")
    gh = FakeGitHub(reviews=[bot_review(OLD_SHA)])
    monkeypatch.setattr(github, "GitHub", lambda token, repo: gh)
    assert cli.main(["post", "--repo", REPO, "--pr", "7", "--dir", str(env / "out")]) == cli.EXIT_FAILED
    assert gh.posted == []


def test_human_review_with_marker_does_not_suppress_the_review(env, monkeypatch):
    planted = {"user": {"login": "dev", "type": "User"}, "state": "COMMENTED",
               "body": f"lgtm\n<!-- agex-review: {SHA} mode=deep -->"}
    api = FakeAPI([GOOD])
    wire(monkeypatch, FakeGitHub(reviews=[planted]), api)
    assert review(env) == cli.EXIT_OK
    assert read_outputs(env / "output")["result"] == "reviewed" and len(api.created) == 1


def test_api_error_while_polling_interrupts_and_retries(env, monkeypatch):
    api = FakeAPI([GOOD, GOOD])
    real_events, calls = api.beta.sessions.events.list, []

    def flaky(session_id):
        calls.append(session_id)
        if len(calls) == 1:
            raise anthropic.APIConnectionError(request=None)
        return real_events(session_id=session_id)
    api.beta.sessions.events.list = flaky
    wire(monkeypatch, FakeGitHub(), api)
    assert review(env) == cli.EXIT_OK
    assert len(api.created) == 2 and api.interrupts == ["sesn_1"]
    record = json.loads((env / "out" / "session.json").read_text())
    assert record["attempts"][0]["errors"][0].startswith("API error: APIConnectionError")


def test_api_errors_on_both_attempts_fail_cleanly(env, monkeypatch):
    api = FakeAPI([GOOD, GOOD])

    def down(session_id):
        raise anthropic.APIConnectionError(request=None)
    api.beta.sessions.events.list = down
    wire(monkeypatch, FakeGitHub(), api)
    assert review(env) == cli.EXIT_FAILED
    assert api.interrupts == ["sesn_1", "sesn_2"]
    assert read_outputs(env / "output")["result"] == "failed"
    assert json.loads((env / "out" / "session.json").read_text())["result"] == "failed"
    assert "sesn_2" in (env / "summary.md").read_text()


def test_timeout_fails_without_retry(env, monkeypatch):
    api = FakeAPI([{"stop": None}, GOOD])
    wire(monkeypatch, FakeGitHub(), api)
    assert review(env, "--deadline-min", "0.001") == cli.EXIT_FAILED
    assert len(api.created) == 1 and api.interrupts == ["sesn_1"]


def test_skip_when_head_already_reviewed(env, monkeypatch):
    api = FakeAPI([GOOD])
    wire(monkeypatch, FakeGitHub(reviews=[bot_review(SHA)]), api)
    assert review(env) == cli.EXIT_OK
    assert read_outputs(env / "output")["result"] == "skipped-reviewed"
    assert api.created == []


def test_shadow_reviews_even_when_head_already_reviewed(env, monkeypatch):
    api = FakeAPI([GOOD])
    wire(monkeypatch, FakeGitHub(reviews=[bot_review(SHA)]), api)
    assert review(env, "--shadow") == cli.EXIT_OK
    assert read_outputs(env / "output")["result"] == "shadow"
    assert "Shadow mode: nothing was posted" in (env / "summary.md").read_text()
    assert cli.main(["post", "--repo", REPO, "--pr", "7", "--dir", str(env / "out")]) == cli.EXIT_OK


def test_fork_pr_is_skipped(env, monkeypatch):
    api = FakeAPI([GOOD])
    wire(monkeypatch, FakeGitHub(pull=pr(fork=True)), api)
    assert review(env) == cli.EXIT_OK
    assert read_outputs(env / "output")["result"] == "skipped-fork"
    assert api.created == []


def test_fork_pr_without_secrets_is_skipped_not_failed(env, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    api = FakeAPI([GOOD])
    wire(monkeypatch, FakeGitHub(pull=pr(fork=True)), api)
    assert review(env) == cli.EXIT_OK
    assert read_outputs(env / "output")["result"] == "skipped-fork"
    assert api.created == []


def test_missing_key_is_a_setup_error(env, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    api = FakeAPI([GOOD])
    wire(monkeypatch, FakeGitHub(), api)
    assert review(env) == cli.EXIT_SETUP
    assert api.created == []


def test_agent_not_deployed_is_a_setup_error(env, monkeypatch):
    wire(monkeypatch, FakeGitHub(), FakeAPI([GOOD], agent=False))
    assert review(env) == cli.EXIT_SETUP


def test_usage_error_exit_code(env):
    with pytest.raises(SystemExit) as e:
        cli.main(["review", "--client", "acme"])
    assert e.value.code == cli.EXIT_USAGE


def reviewed_dir(env, monkeypatch):
    wire(monkeypatch, FakeGitHub(), FakeAPI([GOOD]))
    assert review(env) == cli.EXIT_OK
    return str(env / "out")


def post(d):
    return cli.main(["post", "--repo", REPO, "--pr", "7", "--dir", d])


def test_post_once_then_rerun_posts_nothing(env, monkeypatch):
    d = reviewed_dir(env, monkeypatch)
    gh = FakeGitHub()
    monkeypatch.setattr(github, "GitHub", lambda token, repo: gh)
    assert post(d) == cli.EXIT_OK and post(d) == cli.EXIT_OK
    assert len(gh.posted) == 1
    number, sha, body = gh.posted[0]
    assert sha == SHA and body == review_md()
    assert read_outputs(env / "output")["result"] == "skipped-reviewed"


def test_post_refuses_tampered_outputs(env, monkeypatch):
    d = reviewed_dir(env, monkeypatch)
    (env / "out" / "findings.json").write_text("nope")
    gh = FakeGitHub()
    monkeypatch.setattr(github, "GitHub", lambda token, repo: gh)
    assert post(d) == cli.EXIT_FAILED and gh.posted == []


def test_post_http_error_exit_code(env, monkeypatch):
    d = reviewed_dir(env, monkeypatch)
    err = urllib.error.HTTPError("u", 403, "Forbidden", {}, None)
    monkeypatch.setattr(github, "GitHub", lambda token, repo: FakeGitHub(post_error=err))
    assert post(d) == cli.EXIT_POST


def scored_dir(env, monkeypatch, quality=100, required=0):
    files = {"review.md": review_md(quality=quality), "findings.json": findings(quality, required=required)}
    wire(monkeypatch, FakeGitHub(), FakeAPI([{"stop": "end_turn", "files": files}]))
    assert review(env) == cli.EXIT_OK
    return str(env / "out")


def post_approving(d, min_score="100"):
    return cli.main(["post", "--repo", REPO, "--pr", "7", "--dir", d, "--approve-min-score", min_score])


def wire_post(monkeypatch, gh):
    """Every GitHub client is gh; returns the tokens the clients were made with, in order."""
    tokens = []
    monkeypatch.setattr(github, "GitHub", lambda token, repo: tokens.append(token) or gh)
    return tokens


def test_approve_at_threshold_uses_the_approve_token(env, monkeypatch):
    d = scored_dir(env, monkeypatch)
    monkeypatch.setenv("APPROVE_TOKEN", "owner-token")
    gh = FakeGitHub()
    tokens = wire_post(monkeypatch, gh)
    assert post_approving(d) == cli.EXIT_OK
    assert len(gh.posted) == 1 and len(gh.approved) == 1
    number, sha, body = gh.approved[0]
    assert sha == SHA and body.strip().endswith(f"<!-- agex-approval: {SHA} -->")
    assert tokens == ["t", "owner-token"]
    out = read_outputs(env / "output")
    assert out["result"] == "posted" and out["approved"] == "true"


def test_approve_token_defaults_to_the_github_token(env, monkeypatch):
    d = scored_dir(env, monkeypatch)
    gh = FakeGitHub()
    tokens = wire_post(monkeypatch, gh)
    assert post_approving(d) == cli.EXIT_OK and len(gh.approved) == 1
    assert tokens == ["t", "t"]


@pytest.mark.parametrize("quality,required", [(99, 0), (100, 1)])
def test_no_approval_below_the_score_or_with_required_findings(env, monkeypatch, quality, required):
    d = scored_dir(env, monkeypatch, quality, required)
    gh = FakeGitHub()
    wire_post(monkeypatch, gh)
    assert post_approving(d) == cli.EXIT_OK
    assert len(gh.posted) == 1 and gh.approved == []
    assert read_outputs(env / "output")["approved"] == "false"


def test_lower_threshold_is_per_repo(env, monkeypatch):
    d = scored_dir(env, monkeypatch, quality=95)
    gh = FakeGitHub()
    wire_post(monkeypatch, gh)
    assert post_approving(d, "95") == cli.EXIT_OK and len(gh.approved) == 1


def test_no_approval_unless_the_repo_opts_in(env, monkeypatch):
    d = scored_dir(env, monkeypatch)
    gh = FakeGitHub()
    wire_post(monkeypatch, gh)
    assert post(d) == cli.EXIT_OK
    assert len(gh.posted) == 1 and gh.approved == []
    assert "approved" not in read_outputs(env / "output")


def test_no_approval_when_the_head_moved(env, monkeypatch):
    d = scored_dir(env, monkeypatch)
    gh = FakeGitHub(pull=pr(sha=OLD_SHA))
    wire_post(monkeypatch, gh)
    assert post_approving(d) == cli.EXIT_OK and gh.approved == []


def test_no_approval_when_stale_approvals_survive_pushes(env, monkeypatch, capsys):
    d = scored_dir(env, monkeypatch)
    gh = FakeGitHub(dismisses_stale=False)
    wire_post(monkeypatch, gh)
    assert post_approving(d) == cli.EXIT_OK and gh.approved == []
    assert "::warning::No +1" in capsys.readouterr().out


def test_unreadable_branch_rules_mean_no_approval_with_their_own_warning(env, monkeypatch, capsys):
    d = scored_dir(env, monkeypatch)
    gh = FakeGitHub(dismisses_stale=urllib.error.HTTPError("u", 403, "Forbidden", {}, None))
    wire_post(monkeypatch, gh)
    assert post_approving(d) == cli.EXIT_OK and gh.approved == []
    assert "::warning::No +1: could not read the rules of main: HTTP 403" in capsys.readouterr().out


def test_failed_approval_is_a_warning_not_a_failure(env, monkeypatch, capsys):
    d = scored_dir(env, monkeypatch)
    err = urllib.error.HTTPError("u", 422, "Unprocessable Entity", {}, None)
    gh = FakeGitHub(approve_error=err)
    wire_post(monkeypatch, gh)
    assert post_approving(d) == cli.EXIT_OK
    assert len(gh.posted) == 1 and read_outputs(env / "output")["approved"] == "false"
    assert "::warning::The +1 failed: HTTP 422" in capsys.readouterr().out


def test_rerun_approves_once_and_can_approve_after_an_earlier_failure(env, monkeypatch):
    d = scored_dir(env, monkeypatch)
    gh = FakeGitHub(approve_error=urllib.error.HTTPError("u", 403, "Forbidden", {}, None))
    wire_post(monkeypatch, gh)
    assert post_approving(d) == cli.EXIT_OK and gh.approved == []
    gh.approve_error = None  # e.g. the repo now lets the token approve
    assert post_approving(d) == cli.EXIT_OK and post_approving(d) == cli.EXIT_OK
    assert len(gh.posted) == 1 and len(gh.approved) == 1


def test_approve_min_score_must_be_a_percentage(env):
    with pytest.raises(SystemExit) as e:
        cli.main(["post", "--repo", REPO, "--pr", "7", "--approve-min-score", "101"])
    assert e.value.code == cli.EXIT_USAGE


def test_interrupt_by_session_file(env, monkeypatch):
    api = FakeAPI([{"stop": None}])
    api.beta.sessions.create()
    monkeypatch.setattr(anthropic, "Anthropic", lambda: api)
    (env / "sid").write_text("sesn_1\n")
    assert cli.main(["interrupt", "--session-file", str(env / "sid")]) == cli.EXIT_OK
    assert api.interrupts == ["sesn_1"]


def test_interrupt_with_no_session_is_a_no_op(env):
    assert cli.main(["interrupt", "--session-file", str(env / "absent")]) == cli.EXIT_OK
