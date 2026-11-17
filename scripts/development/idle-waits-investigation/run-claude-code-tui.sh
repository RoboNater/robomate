#!/usr/bin/env bash
# run-claude-code-tui.sh TRIAL HUB PORT WATCH_S (worker NAME T | orch NAME T...)
# Interactive Claude Code in a detached tmux session named TRIAL. The prompt is pasted in after
# start-up; the pane is captured into pane.log every 15 s for WATCH_S seconds, then the session
# is killed. CC_ENV as in run-claude-code.sh (for example CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS=0).
. "$(dirname "$0")/common.sh"
trial=$1; hub=$2; port=$3; watch=$4
shift 4
trial_dir "$trial"
cat > mcp.json <<J
{"mcpServers": {"robomate": {"command": "uv",
  "args": ["run", "--locked", "--project", "$SMOKE_CLONE", "robomate", "mcp", "--harness", "claude-code"],
  "env": {"ROBOMATE_HUB_URL": "http://127.0.0.1:$port",
          "ROBOMATE_TOKEN_FILE": "$(hub_token "$hub")"}}}}
J
"$HERE/prompt.sh" "$@" > prompt.txt
echo "start $(date -u +%FT%TZ) CC_ENV=$CC_ENV (interactive, tmux)" > timing.txt
cat > launch.sh <<L
#!/usr/bin/env bash
cd $d
exec env -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT $CC_ENV claude --model ${CC_MODEL:-claude-haiku-5-5} \\
  --strict-mcp-config --mcp-config mcp.json --allowedTools 'mcp__robomate__*'
L
chmod +x launch.sh
tmux new-session -d -s "$trial" -x 200 -y 50 ./launch.sh
sleep 10
tmux send-keys -t "$trial" Enter   # folder-trust prompt, if shown
sleep 5
tmux load-buffer -b "p$trial" prompt.txt
tmux paste-buffer -b "p$trial" -t "$trial"
sleep 1
tmux send-keys -t "$trial" Enter
end=$((SECONDS + watch))
while [ $SECONDS -lt $end ] && tmux has-session -t "$trial" 2>/dev/null; do
  { echo "===== $(date -u +%FT%TZ)"; tmux capture-pane -p -t "$trial" | grep -v '^\s*$'; } >> pane.log
  sleep 15
done
tmux kill-session -t "$trial" 2>/dev/null
echo "end $(date -u +%FT%TZ)" >> timing.txt
