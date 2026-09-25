# Rebase

You have been assigned a `rebase` task. A reviewer approved a pull request at a
specific commit, but the PR's base branch has moved on since then, or the PR now
conflicts with it. Your job is to bring the PR up to date **without changing
what was approved**, and to report exactly what you had to change to do it.

The assignment gives you the PR URL and `pr_head_sha`, the approved head.

## What happens with your report

Your `conflict_files` list decides what happens next (spec §5 REBASE):

- **Empty** — Alice merges your new head without another review.
- **Not empty** — the new head goes back to the reviewer, who reads the files
  you list.

So an empty list is a claim, not a default. It says that git combined every
file on its own and that you changed nothing else. The claim is checkable:
`git range-diff` between the approved commits and your rebased ones has to show
the approved changes unchanged. If you are unsure whether a file counts, list it.

## Procedure

1. **Start from the approved head.** Check out the PR branch and confirm that
   `git rev-parse HEAD` equals `pr_head_sha`. If it does not, someone else has
   pushed since the approval. Stop and submit `outcome: blocked`, and say so in
   the blocker. Do not rebase a head nobody approved.
2. **Bring in the base.** Fetch the base branch (usually `main`) and either
   merge it into the PR branch (`git merge origin/main`) or rebase onto it. A
   merge keeps the approved commits intact and needs no force-push, so prefer
   it. If you do rebase, push with
   `git push --force-with-lease=<branch>:<pr_head_sha>` so that a push you
   have not seen makes yours fail.
3. **Resolve conflicts, and only conflicts.** Keep the intent of both sides.
   Do not refactor, rename, reformat or fix anything else, however tempting it
   is: every extra change is code no reviewer has read. If a resolution needs a
   design decision, `ask_alice`, or submit `outcome: blocked` with the question
   in `blocker`.
4. **Look for the conflicts git cannot see.** Two branches can merge cleanly
   and still be wrong together. Most often they both claim the same shared
   counter: database schema version, migration number, wire `schema_version`,
   event kind. Take the value reserved for this issue in the roadmap's
   **Reservations** section. Any file you edit for this is a conflict file.
5. **Run the full validation suite**, exactly as CI runs it, not only the
   tests near the conflict. A clean merge can still break the build.
6. **Push and read back the head.** After pushing, confirm the PR's head with
   `gh pr view <pr_url> --json headRefOid`. That value is your `head_sha`.
7. **Submit a `RebaseResult`** with `submit_result`.

## `RebaseResult`

| Field | Meaning |
|---|---|
| `outcome` | `completed`, `blocked` (needs a decision or someone else's action) or `failed` |
| `summary` | One or two sentences: what you merged in and how it went |
| `pr_url` | The PR (optional; Alice already has it) |
| `head_sha` | The PR's new head, 40 hex characters. Required when `completed` |
| `conflict_files` | Every file you edited by hand during the rebase: conflicts git reported, semantic fixes (step 4), and tests you had to change. Empty only if you edited nothing |
| `resolution_summary` | For each conflict file, what conflicted and how you resolved it. Required when `conflict_files` is not empty |
| `tests` | The validation commands you ran and their status |
| `blocker` | What stopped you, when `blocked` |

## Do not

- Address review findings, add features or make unrelated fixes in a rebase
  task. That is a separate implementer task.
- Squash, reorder or reword the approved commits.
- Report a `head_sha` you have not read back from the PR after pushing.
