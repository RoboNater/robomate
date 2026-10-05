#!/bin/bash

# Prep used for the standard run
# Repo commit was:

# The TARGET_REPO is the repo that we are working on.
# A RUN is a single work package (e.g. issue) moving from assigned to merged and completed
TARGET_REPO_ISSUE="105"
RUN_DIR="27-issue-105"
RUN_PARENT_DIR="${HOME}/working"

TARGET_REPO_NAME="robomate"
TARGET_REPO_URL="git@github.com:RoboNater/robomate.git"
FORGE="github"
FORGE_USER_ACCOUNT="RoboNater"
ROADMAP_ISSUE="2"

ROBOMATE_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
ROBOMATE_DIR="$(realpath ROBOMATE_SCRIPT_DIR/..)"

RUN_DIR="${RUN_PARENT_DIR}/${RUN_DIR}"
HUB_DIR="${RUN_DIR}/hub"

mkdir -p "${RUN_DIR}"
# If the hub directory does not exist, clone the repo into it. 
if [ ! -d "${HUB_DIR}" ]; then
	echo "Cloning ${TARGET_REPO_NAME} into ${HUB_DIR}..."
	git clone ${TARGET_REPO_URL} "${HUB_DIR}"
# else print a message that the hub directory already exists and continue
else
	echo "Hub directory ${HUB_DIR} already exists. Skipping clone..."
fi

echo Make sure hub is running in separate terminal.
echo Hub start line should be:
echo     cd ${HUB_DIR} \&\& uv run --project "$(realpath .)" robomate up
echo 
# Prompt user Press any key to continue, or Ctrl-C to abort.
read -p "Press any key to continue, or Ctrl-C to abort." -n1 -s
echo
echo Running the prepare-run script...
echo "------------------------------------------------------------------------------"
uv run --locked python scripts/prepare-run.py \
	--hub-repo "${HUB_DIR}" \
	--repository "${TARGET_REPO_URL}" \
	--run-dir "${RUN_DIR}" \
	--issue ${TARGET_REPO_ISSUE} \
	--account ${FORGE_USER_ACCOUNT} \
	--roadmap ${ROADMAP_ISSUE} \
	                            \
	--alice-harness opencode       \
	--alice-model opencode/muse-spark-1.3-contributor-free \
	--alice-effort medium         \
	                            \
	--bob-harness codex \
      	--bob-model gpt-6.1-sol     \
	--bob-effort medium    \
	                      \
	--charlie-harness claude   \
	--charlie-model claude-opus-5-5 \
	--charlie-effort medium     \
	                         \
	                         \


# Here are some other examples of typical agent configurations
#	--bob-harness claude   \
#	--bob-model opus \
#	--bob-effort high     \

#	--charlie-harness codex \
#      	--charlie-model gpt-6.1-sol     \
#	--charlie-effort medium    \

#	--bob-harness antigravity   \
#	--bob-model gemini-3.8-flash \
#	--bob-effort high     \

