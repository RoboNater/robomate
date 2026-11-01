# GitHub Orchestrator Appendix

Read issues with `gh issue view <number> --repo <owner>/<repository>`.
Read a change request and its discussion with `gh pr view <pr_url> --comments`
and its diff with `gh pr diff <pr_url>`.
For resume and result verification, read the full current head, state, base
and merge commit:

```sh
gh pr view <pr_url> --json headRefOid,state,baseRefName,mergeCommit
```

When the final merge gate invariant holds, checkpoint the event/action and run:

```text
gh pr merge <pr_url> --<merge_method> --delete-branch --match-head-commit <approved head>
```

Read the PR back with the command above. Confirm `state == "MERGED"`, confirm
the recorded head equals the approved head, and record `mergeCommit.oid`
separately. A `--match-head-commit` refusal routes to the gate or RE-REVIEW.
Log the reviewed SHA, any rebase head, and merged SHA as full 40-character SHAs, plus the review-comment URL and every check name/bucket. Never merge from a stale or unreadable gate.

For counter-only reservations close-out, verify the update with
`gh issue view <number> --repo <owner>/<repository>`.
