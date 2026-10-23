"""One review session on Claude Managed Agents: create, wait, interrupt, download the outputs.

Resources are found by name in whichever workspace the API key belongs to: environment
`pr-reviewer-sandbox`, agent `pr-reviewer-<client>`, and optionally `pr-reviewer-<client>-rereview`, the same
reviewer at a lower effort for re-reviews (both deployed from the private pr-reviewer repo).
"""
import time

ENV_NAME = "pr-reviewer-sandbox"
BETAS = ["managed-agents-2026-04-01"]
OUTPUTS = ("review.md", "findings.json")
REPO_MOUNT = "/workspace/repo"
DEADLINE_S = 45 * 60


def agent_name(client, rereview=False):
    return f"pr-reviewer-{client}" + ("-rereview" if rereview else "")


def find_environment(api):
    return next((e for e in api.beta.environments.list() if e.name == ENV_NAME), None)


def find_agent(api, client, rereview=False):
    """The client's agent. A re-review uses the client's re-review agent when one is deployed."""
    agents = {a.name: a for a in api.beta.agents.list()}
    return (rereview and agents.get(agent_name(client, rereview=True))) or agents.get(agent_name(client))


def start(api, *, agent, environment, client, repo, pr, sha, pr_title, clone_token, files, budget_usd):
    """Create the session and return its ID.

    The session clones repo at sha itself through Anthropic's git proxy (clone_token never enters the
    sandbox, so it must be read-only). files maps input name to bytes; each lands at
    /mnt/session/uploads/<name>.
    """
    resources = [{"type": "github_repository", "url": f"https://github.com/{repo}",
                  "authorization_token": clone_token, "mount_path": REPO_MOUNT,
                  "checkout": {"type": "commit", "sha": sha}}]
    for name, data in files.items():
        fid = api.files.upload(file=(name, data), expires_in_seconds=7 * 24 * 3600).id
        resources.append({"type": "file", "file_id": fid, "mount_path": f"/{name}"})
    session = api.beta.sessions.create(
        agent={"type": "agent", "id": agent.id, "version": agent.version},
        environment_id=environment.id,
        title=f"{client} {repo}#{pr}",
        metadata={"client": client, "repo": repo, "pr": str(pr), "sha": sha},
        budget={"type": "limit", "max_list_cost": {"amount": str(round(budget_usd * 100)), "currency": "USD"}},
        resources=resources,
        initial_events=[{"type": "user.message", "content": [{"type": "text", "text":
            f"Review {repo}#{pr} ({pr_title}) at head {sha}. The repo is checked out at {REPO_MOUNT}; the other "
            "inputs are in /mnt/session/uploads/. Follow your instructions and write both output files."}]}],
    )
    return session.id


def wait(api, sid, deadline, poll_s=15):
    """Poll until the session stops or time.monotonic() passes deadline (then interrupt it).

    Returns (status, stop_reason): status is session.status_idle, session.status_terminated or timeout.
    """
    while time.monotonic() < deadline:
        time.sleep(poll_s)
        terminal = [e for e in api.beta.sessions.events.list(session_id=sid)
                    if e.type in ("session.status_idle", "session.status_terminated")]
        if terminal:
            last = terminal[-1]
            stop = getattr(getattr(last, "stop_reason", None), "type", None)
            if last.type == "session.status_terminated" or stop != "requires_action":
                return last.type, stop
    interrupt(api, sid)
    return "timeout", None


def succeeded(status, stop):
    """Only a turn that ended normally can have produced a review; budget_reached, refusal,
    retries_exhausted, terminated and timeout are failures."""
    return status == "session.status_idle" and stop == "end_turn"


def interrupt(api, sid):
    api.beta.sessions.events.send(session_id=sid, events=[{"type": "user.interrupt"}])


def download(api, sid, dest, tries=5):
    """Save review.md and findings.json to dest; the uploaded inputs are listed too but not downloadable."""
    files = []
    for i in range(tries):
        files = [f for f in api.beta.files.list(scope_id=sid, betas=BETAS) if f.filename in OUTPUTS]
        if files or i == tries - 1:
            break
        time.sleep(3)
    for f in files:
        api.beta.files.download(f.id).write_to_file(f"{dest}/{f.filename}")
    return sorted(f.filename for f in files)


def usage(api, sid):
    """(list cost in USD or None, active seconds or None) of the session."""
    u = api.beta.sessions.retrieve(session_id=sid).usage
    cost = float(u.list_cost.amount) / 100 if u and u.list_cost else None
    return cost, u.active_seconds if u else None
