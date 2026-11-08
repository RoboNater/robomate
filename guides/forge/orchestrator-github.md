# GitHub Orchestrator Appendix

Read issues with `gh issue view <number> --repo <owner>/<repository>`.
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

Read the PR back with the command above. Confirm `state == "MERGED"`, confirm
the recorded head equals the approved head, and record `mergeCommit.oid`
separately. A `--match-head-commit` refusal routes to the gate or RE-REVIEW.
Use the read-back state to determine whether the merge happened, including when
the CLI exits nonzero after a successful server-side merge. Once the merged state
and approved head are verified, delete only the remote branch named by
`headRefName`:

```sh
git push origin --delete <branch>
```

Never delete the local branch or its worktree during merge; operator-only
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
