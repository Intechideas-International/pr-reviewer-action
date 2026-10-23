# pr-reviewer-action

GitHub Action for InTech's PR Reviewer. When a pull request is opened or updated, it runs one review of the head commit in a Claude Managed Agents session on the client's reviewer agent, checks the output and posts it as a COMMENT review.

This repo is public because client repos in other orgs can only use public actions. It holds the runner and no secrets. The review method, each client's context, and the deploy script live in the private `Intechideas-International/pr-reviewer` repo.

## How a review runs

The client workflow has two jobs:

1. **review** (`uses: Intechideas-International/pr-reviewer-action@v1`). This job's token is read-only. It reads the PR, the diff, CI status, human reviews and the last bot review. It skips the PR if a bot review for the head commit already exists: a review by a bot account (such as `github-actions[bot]`) whose last line is the `agex-review` marker for that commit. A review by a person never counts, even if it carries the marker. Otherwise it starts a session on agent `pr-reviewer-<client>` in environment `pr-reviewer-sandbox`, and the session clones the repo at the head commit itself. A re-review (the PR already has a bot review of an earlier commit) runs on agent `pr-reviewer-<client>-rereview` instead when the workspace has one: the same reviewer at a lower effort, because a re-review checks the earlier findings and only the changes since. Without that agent, re-reviews use `pr-reviewer-<client>` too. The job waits up to 45 minutes. It then checks `review.md` (template sections, plus the `agex-review` marker for the head commit as the last line) and `findings.json` (`quality` an int from 0 to 100, `required` and `suggested` ints, `findings` a list; on a re-review, no Suggestions or Nits). If the session fails, the output is invalid, or the Anthropic API keeps failing while the job waits (the session is then interrupted), the job tries once more. The result is uploaded as the `pr-review-<number>` artifact, and the session ID and cost go to the job summary.
2. **post** (`uses: Intechideas-International/pr-reviewer-action/post@v1`). This job can write. It runs only when the result is `reviewed`, checks the artifact again, and posts `review.md` unless a bot review for that commit already exists, so re-runs never post twice. In repos that opt in with `approve: true`, it also gives a +1 (see [Approval per repo](#approval-per-repo)).

The review job hands its token to the session, which uses it to clone the repo. That token must be read-only. Anthropic's git proxy also passes GitHub REST calls for the attached repo through with that token, so a write token would let text in a PR get the agent to write to GitHub. That is why posting happens in a separate job. The session's environment has no other network access, and the agent has no web tools.

Each session carries the metadata `client`, `repo`, `pr` and `sha`, and its list cost is capped by `budget-usd`, $15 by default. When a newer commit cancels a run, the run's last step interrupts its session.

Fork PRs are skipped with a notice, because forks get no secrets.

## Set up a client

1. In the Anthropic Console, create a workspace for the client, set its monthly spend limit, and create an API key in it.
2. Deploy the client's agent to that workspace from the private repo: add `clients/<client>/client.json` and `context.md`, then run `deploy.py <client>` with that key (CI does this on merge to main). With `rereview_effort` in `client.json`, the deploy also creates the re-review agent.
3. Add the key to the client's GitHub org (or repo) as the secret `ANTHROPIC_API_KEY`. Do this only after the spend limit is set.
4. Copy [`examples/pr-review.yml`](examples/pr-review.yml) to `.github/workflows/pr-review.yml` in each repo. Set `client`, and adjust the filters in `if:` (authors, base branches, drafts).
5. Optional: add `SLACK_WEBHOOK_URL` as a secret. A failed review or post then sends an alert. Without it, the alert step does nothing.
6. To compare against an existing reviewer first, set `shadow: true`. The review runs and the artifact and job summary are written, but nothing is posted.

### Inputs (review action)

| Input | Default | |
|---|---|---|
| `client` | required | Agent `pr-reviewer-<client>` must exist in the key's workspace |
| `anthropic-api-key` | required | The client's workspace key |
| `github-token` | `github.token` | Read-only: contents, pull-requests, checks, statuses |
| `shadow` | `false` | Review without posting |
| `budget-usd` | `15` | List-cost cap per session |
| `slack-webhook` | empty | Alert on failure when set |

Outputs: `result` (`reviewed`, `shadow`, `failed`, `skipped-fork` or `skipped-reviewed`), `session-id` and `head-sha`.

### Inputs (post action)

| Input | Default | |
|---|---|---|
| `github-token` | `github.token` | Needs pull-requests: write |
| `approve` | `false` | `true` lets the reviewer give a +1 in this repo |
| `approve-min-score` | `100` | Lowest quality score that gets the +1 |
| `approve-token` | empty | Token that gives the +1; empty uses `github-token` |
| `slack-webhook` | empty | Alert on failure when set |

Outputs: `result` (`posted` or `skipped-reviewed`) and `approved` (`true` or `false` when `approve` is on, else empty).

## Approval per repo

Each repo decides in its own `pr-review.yml` whether the reviewer may approve. Some repos can take the reviewer's +1; others need a person's. Because the setting lives in the repo's workflow, changing it takes a PR that the repo's own reviewers approve.

With `approve: true`, the post job gives a +1 only when all of these hold:

- `findings.json` shows a quality of at least `approve-min-score` and no Required findings.
- The PR's head is still the reviewed commit. A newer commit gets its own review.
- A ruleset on the base branch dismisses approvals when new commits are pushed. Otherwise the +1 would still stand after commits the reviewer never saw. Classic branch protection can't be read without admin rights, so a branch protected only that way gets no +1.

The +1 is a separate APPROVE review on the head commit, next to the COMMENT review. It ends with `<!-- agex-approval: <sha> -->`, and later reviews don't count it as a human review. Re-running the job approves at most once, and gives the +1 if an earlier run's attempt failed. A +1 that can't be given is a warning in the job log, not a failed job, because the review itself has already been posted.

Who gives the +1 matters:

- By default it comes from `github-actions[bot]`. That works only if the repo or org setting "Allow GitHub Actions to create and approve pull requests" is on. The approval then counts toward a required number of approvals, but never as a code-owner approval.
- When the base branch requires code-owner review, set `approve-token` to a token of an account in the code-owner team (a machine user, say), stored as a secret in that repo. Its +1 then counts like that person's.

```yaml
      - uses: Intechideas-International/pr-reviewer-action/post@v1
        with:
          approve: true
          approve-token: ${{ secrets.PR_REVIEWER_APPROVE_TOKEN }}
```

## CLI

The actions run the `pr-reviewer` command from this package:

```
pr-reviewer review --client <c> --pr <n> [--repo owner/name] [--shadow] [--budget-usd 15] [--out DIR] [--session-file F]
pr-reviewer post --pr <n> [--repo owner/name] [--dir DIR] [--approve-min-score N]
pr-reviewer interrupt (--session-file F | --session-id ID)
```

`review` needs `ANTHROPIC_API_KEY` and `GITHUB_TOKEN`. `post` needs `GITHUB_TOKEN`, and uses `APPROVE_TOKEN` for the +1 when it is set. Exit codes:

| Code | Meaning |
|---|---|
| 0 | Done: reviewed, posted, shadow, or skipped |
| 1 | The review failed after the retry, or the outputs are invalid |
| 2 | Usage error |
| 3 | Setup error: missing key, or the agent or environment is not deployed |
| 4 | Posting failed |

The private pr-reviewer repo's `review.py` wraps the same library for a local dry run.

## Development

```
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/pytest
```

The tests use fakes for GitHub and Anthropic and make no network calls.
