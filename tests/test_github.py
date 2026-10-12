import json

from fakes import OLD_SHA, SHA, bot_review, pr
from pr_reviewer import github


def human(body="looks fine", login="alice"):
    return {"user": {"login": login, "type": "User"}, "state": "COMMENTED", "body": body}


def test_marker_sha():
    assert github.marker_sha(bot_review(SHA)["body"]) == SHA
    assert github.marker_sha("no marker") is None
    assert github.marker_sha(None) is None


def test_marker_sha_reads_only_the_last_line():
    quoted = bot_review(SHA, f"quoting the diff: <!-- agex-review: {OLD_SHA} mode=deep -->\n")
    assert github.marker_sha(quoted["body"]) == SHA
    assert github.marker_sha(f"<!-- agex-review: {SHA} mode=deep -->\nmore text") is None
    assert not github.already_reviewed([bot_review(OLD_SHA, f"<!-- agex-review: {SHA} x -->\n")], SHA)


def test_a_person_posting_the_marker_is_not_a_bot_review():
    planted = human(f"lgtm\n<!-- agex-review: {SHA} x -->")
    assert not github.already_reviewed([planted], SHA)
    assert github.previous_review([human(f"<!-- agex-review: {OLD_SHA} x -->")], SHA) is None
    assert [r["user"] for r in github.human_reviews([planted])] == ["alice"]


def test_already_reviewed_matches_head_sha_only():
    reviews = [human(), bot_review(OLD_SHA)]
    assert not github.already_reviewed(reviews, SHA)
    assert github.already_reviewed(reviews + [bot_review(SHA)], SHA)


def test_previous_review_is_latest_bot_review_of_an_earlier_commit():
    older = "d" * 40
    reviews = [bot_review(older, "first"), human(), bot_review(OLD_SHA, "second"), bot_review(SHA, "same head")]
    prev = github.previous_review(reviews, SHA)
    assert prev["head_sha"] == OLD_SHA and "second" in prev["body"]
    assert github.previous_review([human(), bot_review(SHA)], SHA) is None


def test_human_reviews_exclude_bot_reviews_and_include_inline_comments():
    out = github.human_reviews([human(), bot_review(SHA)],
                               [{"user": {"login": "bob"}, "path": "a.py", "line": 3, "body": "nit"}])
    assert [r["user"] for r in out] == ["alice", "bob"]
    assert out[1]["path"] == "a.py"


def test_is_fork():
    assert github.is_fork(pr(fork=True))
    assert not github.is_fork(pr())
    deleted = pr()
    deleted["head"]["repo"] = None
    assert github.is_fork(deleted)


def test_get_all_follows_link_pagination(monkeypatch):
    gh = github.GitHub("t", "acme/widgets", api_url="https://api.test")
    pages = {"pulls/7/reviews?per_page=100": ([1, 2], '<https://api.test/next2>; rel="next", <https://api.test/last>; rel="last"'),
             "https://api.test/next2": ([3], "")}
    seen = []

    def request(method, path, body=None, accept=None):
        seen.append(path)
        data, link = pages[path]
        return json.dumps(data).encode(), link
    monkeypatch.setattr(gh, "request", request)
    assert gh.get_all("pulls/7/reviews") == [1, 2, 3]
    assert seen == ["pulls/7/reviews?per_page=100", "https://api.test/next2"]


def test_post_review_is_a_comment_on_the_head_commit(monkeypatch):
    gh = github.GitHub("t", "acme/widgets")
    calls = []
    monkeypatch.setattr(gh, "request", lambda m, p, body=None, accept=None: (calls.append((m, p, body)) or (b"{}", "")))
    gh.post_review(7, SHA, "body")
    assert calls == [("POST", "pulls/7/reviews", {"commit_id": SHA, "body": "body", "event": "COMMENT"})]
