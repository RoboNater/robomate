# GitHub Forge Appendix

GitHub-specific CLI commands using the `gh` tool. The role guide above says
what to do; this appendix says how to do it on GitHub.

## Change requests (pull requests)

- **Read an issue** (implementer, reviewer):
  ```sh
  gh issue view <issue-number-or-url>
  ```
- **Read a change request** (reviewer):
  ```sh
  gh pr view <number-or-url> --comments
  gh pr diff <number>
  ```
  `view` shows the title, body, and discussion; `diff` shows the raw diff.
- **Create pull request** (implementer):
  ```sh
  gh pr create --head <branch> --base <base_branch> --title "..." --body "..."
  ```
- **Verify PR head SHA** (implementer, reviewer, rebase):
  ```sh
  gh pr view --json headRefOid -q .headRefOid
  ```
  Always read the current head SHA back from GitHub before submitting results.
  That read can lag a push by a few seconds: if it does not match the SHA you
  pushed, re-read it before reporting.

## Discussions and reviews

- **Post review comment** (reviewer):
  ```sh
  gh pr comment <pr_url> --body "Reviewer agent <name> on behalf of <account>..."
  ```
  Retain the returned comment URL and report it as `review_url`.
