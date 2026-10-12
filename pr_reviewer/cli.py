"""pr-reviewer: the commands the GitHub Action runs.

    pr-reviewer review --client <c> --pr <n> [--repo owner/name] [--shadow] [--budget-usd 15] [--out DIR]
                       [--session-file F]
    pr-reviewer post --pr <n> [--repo owner/name] --dir DIR
    pr-reviewer interrupt (--session-file F | --session-id ID)

review reads the PR with GITHUB_TOKEN (read-only: the session clones the repo with it), runs one
Managed Agents session on agent pr-reviewer-<client> with ANTHROPIC_API_KEY, retries once if the
session fails or its output is invalid, and leaves review.md, findings.json and session.json in DIR.
post runs in a separate job whose GITHUB_TOKEN can write: it re-validates DIR and posts review.md as a
COMMENT review unless a bot review for that head commit already exists.

Exit codes: 0 done (reviewed, posted, shadow or skipped), 1 review failed, 2 usage, 3 setup
(missing key, agent or environment not deployed), 4 posting failed.
"""
import argparse
import json
import os
import shutil
import sys
import time
import urllib.error

import anthropic

from . import github, session, validate

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_SETUP, EXIT_POST = 0, 1, 2, 3, 4
OUT = "pr-review-out"


def output(**kv):
    """Step outputs, when running in Actions."""
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.writelines(f"{k.replace('_', '-')}={v}\n" for k, v in kv.items())


def summary(text):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text + "\n")


def inputs(gh, pr, reviews):
    """The files the reviewer reads from /mnt/session/uploads/."""
    sha = pr["head"]["sha"]
    meta = {"repo": gh.repo, "number": pr["number"], "title": pr["title"], "body": pr["body"] or "",
            "author": pr["user"]["login"], "base_ref": pr["base"]["ref"], "head_sha": sha, "url": pr["html_url"]}
    context = {"ci": gh.ci(sha),
               "human_reviews": github.human_reviews(reviews, gh.review_comments(pr["number"])),
               "previous_review": github.previous_review(reviews, sha)}
    return {"pr.json": json.dumps(meta, indent=1).encode(), "diff.patch": gh.diff(pr),
            "context.json": json.dumps(context, indent=1).encode()}


def run_review(api, gh, pr, reviews, *, client, out, budget_usd, clone_token, deadline, poll_s=15,
               session_file=None, attempts=2, log=print):
    """Review pr, retrying once. Returns (ok, rows); on ok, review.md and findings.json are in out."""
    agent, env = session.find_agent(api, client), session.find_environment(api)
    if not agent or not env:
        raise LookupError(f"agent {session.agent_name(client)} or environment {session.ENV_NAME} "
                          "not found in this API key's workspace: deploy the client first")
    sha, files, rows = pr["head"]["sha"], inputs(gh, pr, reviews), []
    for n in range(1, attempts + 1):
        if time.monotonic() >= deadline:
            break
        dest = os.path.join(out, f"attempt-{n}")
        os.makedirs(dest, exist_ok=True)
        sid = session.start(api, agent=agent, environment=env, client=client, repo=gh.repo, pr=pr["number"],
                            sha=sha, pr_title=pr["title"], clone_token=clone_token, files=files,
                            budget_usd=budget_usd)
        if session_file:
            open(session_file, "w").write(sid)
        output(session_id=sid)
        log(f"attempt {n}: session {sid} started")
        try:
            status, stop = session.wait(api, sid, deadline, poll_s)
        except KeyboardInterrupt:
            session.interrupt(api, sid)
            raise
        saved = session.download(api, sid, dest)
        cost, secs = session.usage(api, sid)
        errors = validate.validate(dest, sha) if session.succeeded(status, stop) else [f"session ended {status}/{stop}"]
        rows.append({"attempt": n, "session_id": sid, "status": status, "stop_reason": stop, "cost_usd": cost,
                     "active_seconds": secs, "files": saved, "errors": errors})
        log(f"attempt {n}: {status}/{stop} cost ${cost} files {saved} " + ("OK" if not errors else "; ".join(errors)))
        if not errors:
            for name in session.OUTPUTS:
                shutil.copy(os.path.join(dest, name), out)
            return True, rows
        if status == "timeout":
            break
    return False, rows


def write_record(out, pr, rows, result):
    json.dump({"pr": {"repo": pr["base"]["repo"]["full_name"], "number": pr["number"], "head_sha": pr["head"]["sha"]},
               "result": result, "attempts": rows}, open(os.path.join(out, "session.json"), "w"), indent=1, default=str)


def review_summary(repo, pr, sha, rows, result):
    lines = [f"### PR Reviewer: {repo}#{pr} at `{sha[:8]}`", "", f"Result: **{result}**", ""]
    if result == "shadow":
        lines += ["Shadow mode: nothing was posted. review.md and findings.json are in the workflow artifacts.", ""]
    if rows:
        lines += ["| Attempt | Session | Outcome | Cost | Active s | Problems |", "|---|---|---|---|---|---|"]
        for r in rows:
            cost = "n/a" if r["cost_usd"] is None else f"${r['cost_usd']:.2f}"
            lines.append(f"| {r['attempt']} | `{r['session_id']}` | {r['status']}/{r['stop_reason']} | {cost} | "
                         f"{r['active_seconds'] or 'n/a'} | {'; '.join(r['errors']) or 'none'} |")
    return "\n".join(lines)


def cmd_review(a):
    if not os.environ.get("GITHUB_TOKEN"):
        print("::error::GITHUB_TOKEN is not set")
        return EXIT_SETUP
    gh = github.GitHub(os.environ["GITHUB_TOKEN"], a.repo)
    pr = gh.pr(a.pr)
    sha = pr["head"]["sha"]
    output(head_sha=sha)
    if github.is_fork(pr):
        print("::notice::PR comes from a fork; the reviewer does not run on fork PRs.")
        output(result="skipped-fork")
        return EXIT_OK
    reviews = gh.reviews(a.pr)
    if github.already_reviewed(reviews, sha) and not a.shadow:
        print(f"::notice::A bot review for {sha[:8]} already exists; nothing to do.")
        output(result="skipped-reviewed")
        return EXIT_OK
    if not os.environ.get("ANTHROPIC_API_KEY"):  # checked after the skips: fork PRs get no secrets
        print("::error::ANTHROPIC_API_KEY is not set")
        return EXIT_SETUP

    api = anthropic.Anthropic()
    os.makedirs(a.out, exist_ok=True)
    try:
        ok, rows = run_review(api, gh, pr, reviews, client=a.client, out=a.out, budget_usd=a.budget_usd,
                              clone_token=os.environ["GITHUB_TOKEN"], session_file=a.session_file,
                              deadline=time.monotonic() + a.deadline_min * 60, poll_s=a.poll_seconds)
    except LookupError as e:
        print(f"::error::{e}")
        return EXIT_SETUP
    result = ("shadow" if a.shadow else "reviewed") if ok else "failed"
    write_record(a.out, pr, rows, result)
    output(result=result)
    summary(review_summary(a.repo, a.pr, sha, rows, result))
    if not ok:
        print(f"::error::Review of {a.repo}#{a.pr} at {sha[:8]} failed after {len(rows)} attempt(s); nothing posted.")
        return EXIT_FAILED
    return EXIT_OK


def cmd_post(a):
    record = json.load(open(os.path.join(a.dir, "session.json")))
    sha = record["pr"]["head_sha"]
    if record["result"] != "reviewed":
        print(f"::notice::Nothing to post (review result: {record['result']}).")
        return EXIT_OK
    errors = validate.validate(a.dir, sha)
    if errors:
        print("::error::Not posting, outputs are invalid: " + "; ".join(errors))
        return EXIT_FAILED
    gh = github.GitHub(os.environ["GITHUB_TOKEN"], a.repo)
    try:
        if github.already_reviewed(gh.reviews(a.pr), sha):
            print(f"::notice::A bot review for {sha[:8]} already exists; not posting again.")
            output(result="skipped-reviewed")
            return EXIT_OK
        posted = gh.post_review(a.pr, sha, open(os.path.join(a.dir, "review.md"), encoding="utf-8").read())
    except urllib.error.HTTPError as e:
        print(f"::error::Posting the review failed: HTTP {e.code} {e.reason}")
        return EXIT_POST
    print(f"posted {posted.get('html_url')}")
    output(result="posted")
    summary(f"Posted the review for `{sha[:8]}`: {posted.get('html_url')}")
    return EXIT_OK


def cmd_interrupt(a):
    sid = a.session_id
    if not sid and a.session_file and os.path.isfile(a.session_file):
        sid = open(a.session_file).read().strip()
    if not sid:
        print("no session to interrupt")
        return EXIT_OK
    session.interrupt(anthropic.Anthropic(), sid)
    print(f"interrupted {sid}")
    return EXIT_OK


def main(argv=None):
    ap = argparse.ArgumentParser(prog="pr-reviewer", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    repo = {"default": os.environ.get("GITHUB_REPOSITORY"), "help": "owner/name (default: GITHUB_REPOSITORY)"}

    r = sub.add_parser("review")
    r.add_argument("--client", required=True)
    r.add_argument("--repo", **repo)
    r.add_argument("--pr", type=int, required=True)
    r.add_argument("--shadow", action="store_true")
    r.add_argument("--budget-usd", type=float, default=15)
    r.add_argument("--out", default=OUT)
    r.add_argument("--session-file")
    r.add_argument("--deadline-min", type=float, default=session.DEADLINE_S / 60)
    r.add_argument("--poll-seconds", type=float, default=15)

    p = sub.add_parser("post")
    p.add_argument("--repo", **repo)
    p.add_argument("--pr", type=int, required=True)
    p.add_argument("--dir", default=OUT)

    i = sub.add_parser("interrupt")
    i.add_argument("--session-file")
    i.add_argument("--session-id")

    a = ap.parse_args(argv)
    if a.cmd in ("review", "post") and not a.repo:
        ap.error("--repo is required outside GitHub Actions")
    return {"review": cmd_review, "post": cmd_post, "interrupt": cmd_interrupt}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
