#!/usr/bin/env bash
set -euo pipefail

mode=${1:-all}
if [[ $# -gt 1 || ! $mode =~ ^(all|--with-claude|--claude|--codex)$ ]]; then
  echo "Usage: $0 [--with-claude|--claude|--codex]" >&2
  exit 2
fi
fail() { echo "Sandbox check failed: $*. See docs/user-guide.md#linux-sandbox-grants" >&2; exit 1; }
[[ $(command -v bwrap || true) == /usr/bin/bwrap ]] || fail 'bwrap must be /usr/bin/bwrap on PATH'
bwrap --ro-bind / / --dev /dev --proc /proc --unshare-user --unshare-pid unshare -U -r true || fail 'nested user namespace'
if unshare -U -r true >/dev/null 2>&1; then
  echo 'Outside-bwrap user namespaces are available (informational).'
else
  echo 'Outside-bwrap user namespaces are refused (informational).'
fi
if [[ $mode != --claude ]] && command -v codex >/dev/null 2>&1; then
  codex sandbox -- /bin/true || fail 'codex sandbox startup'
  scratch=$(mktemp -d)
  trap 'rm -rf -- "$scratch"' EXIT
  # A unique filename prevents an existing file from changing the result.
  probe="$HOME/.robomate-sandbox-probe-$(basename "$scratch")"
  [[ ! -e $probe ]] || fail 'probe path already exists'
  trap 'rm -f -- "$probe"; rm -rf -- "$scratch"' EXIT
  codex sandbox -P :workspace -C "$scratch" -- sh -c 'touch "$PWD/x" && ! touch "$1"' sh "$probe" || fail 'codex workspace / HOME write boundary'
  [[ ! -e $probe ]] || fail "codex wrote $probe; remove it manually"
fi
if [[ $mode != --claude ]] && ! command -v codex >/dev/null 2>&1; then
  echo "codex not on PATH; Codex checks skipped." >&2
fi
if [[ $mode == --codex ]] && ! command -v codex >/dev/null 2>&1; then
  fail 'codex is not on PATH'
fi
if [[ $mode == --with-claude ]]; then
  command -v claude >/dev/null 2>&1 || fail 'claude is not on PATH'
  scratch=${scratch:-$(mktemp -d)}
  trap 'rm -rf -- "$scratch"' EXIT
  git -C "$scratch" init -q
  probe="$HOME/.robomate-claude-probe-$(basename "$scratch")"
  [[ ! -e $probe ]] || fail 'Claude probe path already exists'
  trap 'rm -f -- "$probe"; rm -rf -- "$scratch"' EXIT
  output=$(cd "$scratch" && claude -p --model haiku --settings '{"sandbox":{"enabled":true,"allowUnsandboxedCommands":false,"failIfUnavailable":true}}' --permission-mode auto "Run this exact shell command and return its output verbatim: git status --short && echo ROBOMATE_GIT_OK; if touch '$probe'; then echo ROBOMATE_HOME_WRITABLE; else echo ROBOMATE_HOME_REFUSED; fi") || fail 'Claude Code probe'
  printf '%s\n' "$output"
  [[ $output == *ROBOMATE_GIT_OK* && $output == *ROBOMATE_HOME_REFUSED* && $output != *ROBOMATE_HOME_WRITABLE* && ! -e $probe ]] || fail 'Claude Code did not confirm git and HOME boundary'
fi
echo 'Sandbox checks passed.'
