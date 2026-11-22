# GitHub Orchestrator Appendix

Write every Markdown body (PR description, comment, issue body) to a uniquely
named file in the agent's own temporary directory — never a shared `/tmp` path
(#3's incident uploaded a stale file from an earlier run) — and pass
that file with `--body-file`. Never pass Markdown inline. This applies on both
POSIX shells and PowerShell.

Read issues, including their full discussion, with
`gh issue view <number> --repo <owner>/<repository> --json title,body,comments`.
When its output is piped or redirected, `--comments` alone prints only the
comments; the `--json` form returns the body and every comment together.
Read a change request and its discussion with `gh pr view <pr_url> --comments`
and its diff with `gh pr diff <pr_url>`.
For resume and result verification, read the full current head, state, base
and merge commit:

```sh
gh pr view <pr_url> --json headRefOid,headRefName,state,baseRefName,mergeCommit
```

When the final merge gate invariant holds, checkpoint the event/action and run:

```text
gh pr merge <pr_url> --<merge_method> --match-head-commit <approved head>
```

Read the PR back with `check_merge_gate(pr_url, expected_head_sha=<approved head>)`.
Require `pr_state == merged`, `head_matches` and a non-null `merged_sha` before
WRAP-UP; that gate reading is what records the forge's target commit in the hub.
A `--match-head-commit` refusal routes to the gate or RE-REVIEW. Use the gate's
`pr_state` to determine whether the merge happened, including when the CLI exits
nonzero after a successful server-side merge. When the gate cannot answer, fall
back to `gh pr view`: confirm `state == "MERGED"`, the recorded head equals the
approved head, and read `mergeCommit.oid`, logging that this fallback does not
record `merged_sha` in the hub. Use `gh pr view` for the `headRefName` branch-name
lookup below too; it never substitutes for the gate reading. Once the merged state
and approved head are verified, delete only the remote branch named by
`headRefName`:

```sh
gh pr view <pr_url> --json headRefName -q .headRefName
git push origin --delete <branch>
```

Never use `gh pr merge --delete-branch` in a linked worktree: it can try to
remove another agent’s worktree. Never delete the local branch or its worktree
during merge; operator-only
`robomate clean` owns that cleanup. If remote deletion fails, check the exact ref:

```sh
git ls-remote --exit-code origin refs/heads/<branch>
```

Exit 2 means no matching ref: count the branch as already deleted (for example,
GitHub may have deleted it automatically). Exit 0 means it still exists; report
an outstanding cleanup item. Any other exit means the lookup failed: report
cleanup as unverified. Keep the verified merge result in every case; do not
retry the merge merely to delete a branch.
Log the reviewed SHA, any rebase head, and merged SHA as full 40-character SHAs, plus the review-comment URL and every check name/bucket. Never merge from a stale or unreadable gate.

For counter-only reservations close-out, verify the update with
`gh issue view <number> --repo <owner>/<repository>`.
