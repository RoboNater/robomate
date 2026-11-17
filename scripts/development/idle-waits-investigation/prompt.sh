#!/usr/bin/env bash
# prompt.sh worker NAME TIMEOUT | orch NAME TIMEOUT... — print the trial prompt.
kind=$1; name=$2; shift 2
if [ "$kind" = worker ]; then cat <<P
This is a test of how long one MCP tool call can be held. Use only the robomate MCP server's tools; run no shell commands and edit no files.
1. Call the robomate tool join with name="$name" and role="implementer".
2. Call the robomate tool await_assignment with timeout_s=$1, exactly once. Wait in the foreground for its result, however long it takes. An empty result ({"timeout": true}) is the expected outcome. Do not call it again and do not call any other tool while it runs.
3. Then reply with a short report: the exact result or error text of step 2, and whether anything unusual happened (the call was moved to the background, cancelled, timed out on the client side, or you were asked to do something else).
P
else cat <<P
This is a test of how long one MCP tool call can be held. Use only the robomate MCP server's tools; run no shell commands and edit no files.
1. Call the robomate tool join with name="$name", role="orchestrator" and takeover=true.
2. Call start_workflow with statement="Smoke workflow for #497 hold trials. No work is to be done." and policy={"max_wall_minutes": 600}. If it reports an error, note it and continue.
3. For each of these timeout values in order: $*
   call wait_for_event with timeout_s set to that value and wait in the foreground for the result, however long it takes. If the result contains an event, call wait_for_event again with the same timeout_s and ack set to that event's delivery_id, until a call returns {"event": null}; then move to the next value. Never call any other tool while a wait is running.
4. Then reply with a short report: for each timeout value, the exact result or error text, and whether anything unusual happened (the call was moved to the background, cancelled, timed out on the client side, or you were asked to do something else).
P
fi
