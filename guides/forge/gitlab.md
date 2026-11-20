# GitLab Forge Appendix

GitLab-specific CLI commands using the `glab` tool (minimum version 1.36.0)
and the REST API through `glab api`. The role guide above says what to do;
this appendix says how to do it on GitLab.

`<host>` is the GitLab host from the hub's origin project, `<project>` is that
project's path (for example, `RoboNater/robomate-glab-sandbox`), and
`<group%2Fproject>` is its URL-encoded form
(`RoboNater%2Frobomate-glab-sandbox`). Pass `--hostname <host>` on every
`glab api` call, and `-R <host>/<project>` wherever a `glab` subcommand
selects the repository.

Write every Markdown body (MR description, review note, issue body) to a
uniquely named file in the agent's own temporary directory — never a shared
`/tmp` path (#3's roadmap incident uploaded a stale file from an earlier run) —
and pass that file with `-F <field>@<file>` (for example `-F
description=@mr-description.md`). Never pass Markdown inline. This applies on
both POSIX shells and PowerShell.

## Change requests (merge requests)

- **Read an issue** (implementer, reviewer), including its full discussion:
  ```sh
  glab issue view <issue-number-or-url> -R <host>/<project> --comments --per-page 100
  ```
  `--comments` returns one page of notes (20 by default). Read further pages
  with `--page 2`, `--page 3`, ... until a page comes back short, so a note
  beyond the first page is not missed. Confirm the body and every note came
  back before relying on the read.
- **Read a change request** (reviewer):
  ```sh
  glab mr view <iid> -R <host>/<project> --comments
  glab mr diff <iid> -R <host>/<project>
  ```
  `view` shows the title, body, and discussion; `diff` shows the raw diff.
- **Create merge request** (implementer; `glab mr create --help` on the
  minimum 1.36.0 has no description-from-file flag, so create through the
  REST API with the same `@file` form the review-note recipe uses):
  ```sh
  glab api --hostname <host> --method POST projects/<group%2Fproject>/merge_requests -F source_branch=<branch> -F target_branch=<base_branch> -F title=<title> -F description=@mr-description.md
  ```
  Read `.iid` and `.web_url` from the response.
- **Verify MR head SHA** (implementer, reviewer, rebase):
  `glab mr view` does not output raw JSON, so read the head SHA via `glab api`:
  ```sh
  glab api projects/<group%2Fproject>/merge_requests/<iid> --hostname <host>
  ```
  Read the top-level `.sha` property from the returned JSON. Always verify the
  SHA from GitLab before submitting results. That read can lag a push by a few
  seconds, like the GitHub read: if it does not match the SHA you pushed,
  re-read it before reporting.

## Discussions and reviews

- **Post review note** (reviewer) through the Notes REST API, which is part of
  GitLab's stable REST API and does not change with `glab`'s subcommand layout:
  ```sh
  glab api --hostname <host> --method POST projects/<group%2Fproject>/merge_requests/<iid>/notes -F body=@review.md
  ```
  A note response carries an `id` but no `web_url`. Take the `id` from the
  response and build `ReviewerResult.review_url` as `<MR URL>#note_<id>`. Read
  the note back by that `id` before reporting it:
  ```sh
  glab api --hostname <host> projects/<group%2Fproject>/merge_requests/<iid>/notes/<id>
  ```
  confirming `resolvable: false`. Top-level notes are non-resolvable and can
  never become a `discussions_not_resolved` merge blocker. Do not create
  resolvable discussion threads for review comments.
