#!/usr/bin/env bash
# Editable example for one standard run. Prefer prep-standard-run.sh + TOML
# when sharing/reusing settings. Environment overrides make this example usable
# without editing it; WORK_FILE selects a statement of work instead of ISSUE.
set -euo pipefail

robomate_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
# A config selects the configurable wrapper, without the example's pinned
# settings masking its work source or other values.
for argument in "$@"; do
    case "${argument}" in
        --config|--config=*) exec "${robomate_root}/scripts/prep-standard-run.sh" "$@" ;;
    esac
done

TARGET_REPO_ISSUE="${TARGET_REPO_ISSUE-105}"
WORK_FILE="${WORK_FILE-}"
RUN_DIR="${RUN_DIR-27-issue-105}"
RUN_PARENT_DIR="${RUN_PARENT_DIR-${HOME}/working}"
TARGET_REPO_URL="${TARGET_REPO_URL-git@github.com:RoboNater/robomate.git}"
FORGE="${FORGE-github}"
FORGE_USER_ACCOUNT="${FORGE_USER_ACCOUNT-RoboNater}"
ROADMAP_ISSUE="${ROADMAP_ISSUE-2}"

work_args=(--issue "${TARGET_REPO_ISSUE}")
if [[ -n "${WORK_FILE}" ]]; then
    work_args=(--work-file "${WORK_FILE}")
fi
for argument in "$@"; do
    case "${argument}" in
        --issue|--issue=*|--work-file|--work-file=*) work_args=() ;;
    esac
done

exec "${robomate_root}/scripts/prep-standard-run.sh" \
    --repository "${TARGET_REPO_URL}" \
    --run-parent-dir "${RUN_PARENT_DIR}" --run-dir "${RUN_DIR}" \
    "${work_args[@]}" --account "${FORGE_USER_ACCOUNT}" \
    --forge "${FORGE}" --roadmap "${ROADMAP_ISSUE}" \
    --alice-harness opencode \
    --alice-model opencode/muse-spark-1.3-contributor-free --alice-effort medium \
    --bob-harness codex --bob-model gpt-6.1-sol --bob-effort medium \
    --charlie-harness claude --charlie-model claude-opus-5-5 --charlie-effort medium \
    "$@"
