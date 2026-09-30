# GitLab Forge Appendix

This appendix specifies GitLab-specific CLI commands using the `glab` tool and REST API.

## Change requests (Merge Requests)

- **Create merge request** (implementer):
  ```sh
  glab mr create -R <project_repo> --source-branch <branch> --target-branch <base_branch> --title "..." --description "..." --yes
  ```
- **Verify MR head SHA** (implementer, rebase):
  `glab mr view` does not output raw JSON, so read the head SHA via `glab api` using the URL-encoded project path (e.g. `RoboNater%2Frobomate-glab-sandbox`):
  ```sh
  glab api projects/<group%2Fproject>/merge_requests/<iid> --hostname <host>
  ```
  Read the top-level `.sha` property from the returned JSON. Always verify the SHA from GitLab before submitting results.

## Discussions and reviews

- **Post review note** (reviewer):
  ```sh
  glab mr note <iid> -R <project_repo> -m "Reviewer agent <name> on behalf of <account>..."
  ```
  Top-level MR notes created via `glab mr note` are non-resolvable (`resolvable: false`). Do not create resolvable discussion threads for review comments, as unresolved threads can block merges.

## Merging

- **SHA-bound merge** (Alice / orchestrator):
  ```sh
  glab mr merge <iid> -R <project_repo> --sha <approved_head_sha> --auto-merge=false [--squash] --remove-source-branch --yes
  ```
  The merge is strictly bound to `<approved_head_sha>` via `--sha`. Always pass `--auto-merge=false` to prevent scheduling unverified auto-merges, and pass `--yes` to ensure non-interactive execution.
