# GitHub Forge Appendix

GitHub-specific CLI commands using the `gh` tool. The role guide above says
what to do; this appendix says how to do it on GitHub.

Write every Markdown body (PR description, review comment, issue body) to a
uniquely named file in the agent's own temporary directory — never a shared
`/tmp` path (#3's roadmap incident uploaded a stale file from an earlier run) —
and pass that file with `--body-file`. Never pass Markdown inline. This applies
on both POSIX shells and PowerShell.

## Change requests (pull requests)

- **Read an issue** (implementer, reviewer), including its full discussion:
  ```sh
  gh issue view <issue-number-or-url> --json title,body,comments
  ```
  `gh issue view --comments` prints only the comments when its output is piped
  or redirected. The `--json` form above returns the body and every comment
  together; confirm both came back before relying on the read.
- **Read a change request** (reviewer):
  ```sh
  gh pr view <number-or-url> --comments
  gh pr diff <number>
  ```
  `view` shows the title, body, and discussion; `diff` shows the raw diff.
- **Create pull request** (implementer):
  ```sh
  gh pr create --head <branch> --base <base_branch> --title "..." --body-file <file>
  ```
- **Verify PR head SHA** (implementer, reviewer, rebase):
  ```sh
  gh pr view <number-or-url> --json headRefOid -q .headRefOid
  ```
  Always read the current head SHA back from GitHub before submitting results.
  Name the change request explicitly: without an argument `gh` reads the
  current branch, which fails on a detached checkout. That read can lag a push
  by a few seconds: if it does not match the SHA you pushed, re-read it before
  reporting.

## Discussions and reviews

- **Post review comment** (reviewer):
  ```sh
  gh pr comment <pr_url> --body-file <file>
  ```
  Write the comment body (starting with `Reviewer agent <name> on behalf of
  <account>...`) to the file first. Retain the returned comment URL and report
  it as `review_url`.
