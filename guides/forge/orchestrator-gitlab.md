# GitLab Orchestrator Appendix

Write every Markdown body (MR description, note, issue body) to a uniquely
named file in the agent's own temporary directory — never a shared `/tmp` path
(#3's incident uploaded a stale file from an earlier run) — and pass
that file with `-F <field>=@<file>` (for example `-F description=@<file>`).
Never pass Markdown inline. This applies on both POSIX shells and PowerShell.

When the forge is GitLab, checkpoint the event/action and run:

```text
glab mr merge <iid> -R <host>/<project> --sha <approved head> --auto-merge=false --squash/--squash=false --remove-source-branch --yes
```

Use `--squash` when `policy.merge_method == "squash"`, and `--squash=false` when
`policy.merge_method == "merge"`.

Read the MR back with `check_merge_gate(pr_url, expected_head_sha=<approved head>)`.
Require `pr_state == merged`, `head_matches` and a non-null `merged_sha` before
WRAP-UP; that gate reading is what records the forge's target commit in the hub.
`glab api` and `glab mr view` are only a fallback when the gate cannot answer;
they never substitute for the gate reading. Log the
reviewed SHA, any rebase head, and merged SHA as full 40-character SHAs, plus the
review-comment URL and every check name/bucket. Never merge from a stale or
unreadable gate.

When the gate cannot answer, read the MR back from GitLab
(`glab api --hostname <host> projects/<group%2Fproject>/merge_requests/<iid>`).
Confirm `state == "merged"`, record the merge commit SHA (`merge_commit_sha`) or
squash commit SHA (`squash_commit_sha`) separately (`glab mr view` does not
output these commit SHAs), and log that this fallback does not record `merged_sha`
in the hub.

A `--sha` 409 conflict and a "Pipelines must succeed" refusal are the same safe
outcome as a `--match-head-commit` refusal: GitLab refused to merge an unverified
or unready head, protecting the target branch; route back to the gate or
RE-REVIEW rather than forcing or bypassing the check.

After the merged read-back succeeds and before WRAP-UP, post the merge-record
comment the MERGE section requires through the Notes REST API:

```text
glab api --hostname <host> --method POST projects/<group%2Fproject>/merge_requests/<iid>/notes -F body=@<file>
```

Start it as `Orchestrator agent <name> on behalf of <account>` and say it is the
orchestrator's merge record. Include the reviewed head (full SHA) with the
review-comment URL of the final approving review (the last approving
`ReviewerResult` before merge; a clean rebase adds no review), and the approved
head when it differs because a clean rebase retained approval at the rebased
head. Include any CI failure on a reviewed or approved head along the path, its
diagnosis and repair or re-run, any rebase's head before -> after and base
before and after and new head with `conflict_files` empty or the focused
re-review URL, any operator decision that changed the path with its recorded
`question_id` and a neutral summary, the final check names and results, and the
merged SHA. One or two lines with the approved head, review URL and merged SHA
suffice when the path was simple. For `deliver: pr`, post the same record at
WRAP-UP with no merged SHA and say the PR remains unmerged by policy.

### GitLab CLI equivalents for reads

- **Read an issue** (kickoff, plan), including its full discussion:
  ```sh
  glab issue view <iid> -R <host>/<project> --comments --per-page 100
  ```
  `--comments` returns one page of notes (20 by default). Read further pages
  with `--page 2`, `--page 3`, ... until a page comes back short.
- **Read a change request** (resume, review verification):
  ```sh
  glab mr view <iid> -R <host>/<project> --comments
  glab mr diff <iid> -R <host>/<project>
  ```
- **Verify reservations issue update** (close-out):
  ```sh
  glab issue view <number> -R <host>/<project>
  ```
  (use `gh issue view` if the reservations issue is hosted on GitHub).

For resume and head verification, use the MR API above; read `.sha`, `.state`,
`.target_branch`, `.merge_commit_sha` and `.squash_commit_sha`. Compare `.sha`
with the approved head before routing a recovered merged MR to close-out.
