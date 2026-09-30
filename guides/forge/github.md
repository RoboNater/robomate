# GitHub Forge Appendix

This appendix specifies GitHub-specific CLI commands using the `gh` tool.

## Change requests (Pull Requests)

- **Create pull request** (implementer):
  ```sh
  gh pr create --head <branch> --base <base_branch> --title "..." --body "..."
  ```
- **Verify PR head SHA** (implementer, rebase):
  ```sh
  gh pr view --json headRefOid -q .headRefOid
  ```
  Always read the current head SHA back from GitHub before submitting results.

## Discussions and reviews

- **Post review comment** (reviewer):
  ```sh
  gh pr comment <pr_url> --body "Reviewer agent <name> on behalf of <account>..."
  ```

## Merging

- **SHA-bound merge** (Alice / orchestrator):
  ```sh
  gh pr merge <pr_url> --<method> --delete-branch --match-head-commit <approved_head_sha>
  ```
  The merge is strictly bound to `<approved_head_sha>` via `--match-head-commit`.
