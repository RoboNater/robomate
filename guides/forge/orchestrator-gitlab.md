# GitLab Orchestrator Appendix

When the forge is GitLab, checkpoint the event/action and run:

```text
glab mr merge <iid> -R <host>/<project> --sha <approved head> --auto-merge=false --squash/--squash=false --remove-source-branch --yes
```

Use `--squash` when `policy.merge_method == "squash"`, and `--squash=false` when
`policy.merge_method == "merge"`.

Then read the MR back from GitLab
(`glab api --hostname <host> projects/<group%2Fproject>/merge_requests/<iid>`).
Confirm `state == "merged"`. Record the merge commit SHA (`merge_commit_sha`) or
squash commit SHA (`squash_commit_sha`) separately (`glab mr view` does not
output these commit SHAs). Log the reviewed SHA, any rebase head, merged SHA,
review-comment URL, and every check name/bucket. Never merge from a stale or
unreadable gate.

A `--sha` 409 conflict and a "Pipelines must succeed" refusal are the same safe
outcome as a `--match-head-commit` refusal: GitLab refused to merge an unverified
or unready head, protecting the target branch; route back to the gate or
RE-REVIEW rather than forcing or bypassing the check.

### GitLab CLI equivalents for reads

- **Read an issue** (kickoff, plan):
  ```sh
  glab issue view <iid> -R <host>/<project>
  ```
- **Read a change request** (resume, review verification):
  ```sh
  glab mr view <iid> -R <host>/<project> --comments
  glab mr diff <iid> -R <host>/<project>
  ```
- **Verify roadmap issue update** (close-out):
  ```sh
  glab issue view <number> -R <host>/<project>
  ```
  (use `gh issue view` if the roadmap issue is hosted on GitHub).

For resume and head verification, use the MR API above; read `.sha`, `.state`,
`.target_branch`, `.merge_commit_sha` and `.squash_commit_sha`. Compare `.sha`
with the approved head before routing a recovered merged MR to close-out.
