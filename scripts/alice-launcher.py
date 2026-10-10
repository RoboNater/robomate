#!/usr/bin/env python3
"""Run a headless Alice, resuming the same conversation until her workflow is done (#146, #158).

A headless orchestrator (``claude -p``, ``codex exec``, ``opencode run``,
``agy -p``) exits when the model ends its turn, and some models end it early
whatever the skill says. Nothing else restarts her, so the workflow sits idle.
Both launchers share ``agent_launcher.py`` (#165):

1. It launches the harness once with the kickoff prompt (``--prompt``), or, for
   a manual resume, resumes ``--resume-session`` with ``--resume-prompt``.
2. It records the exact conversation ID: one it chose (Claude Code
   ``--session-id``) or the one the harness prints (Codex ``session id:``,
   OpenCode ``sessionID``, AntiGravity's stream-json conversation ID). It never
   resumes an unspecified "latest" conversation.
3. When the harness exits, it reads the run hub with bearer-only ``hub.info``
   and ``hub.status``, pinned to the hub and workflow it first saw. It never
   opens an orchestrator session, reads ``.robomate/``, or changes hub state.
4. A ``done`` workflow ends it successfully. ``active`` or ``escalated`` resumes
   the same conversation with one fixed continuation prompt that carries no
   state and no operator decision, backing off between resumes (#158): the wait
   doubles from ``--resume-delay-s`` up to ``--resume-max-delay-s`` (about 30
   min), then repeats at the cap. A run lasting ``--resume-series-reset-s``
   starts a new series at the short delay. Resumes stop after
   ``--resume-total-s`` (about 12 h) since the first exit of the current
   series, or after ``--max-resumes`` when given
   (by default no count limit). ``paused`` stops it until the operator resumes
   by hand. Healthy running time before that first exit never spends the
   budget, and a series reset refills it.

It stops, without launching again, when it cannot tell what happened: the hub
stays unreadable or reports another hub or workflow after ``--read-retries``
reads, the workflow was never initialized, or the conversation ID never
appeared. It does not watch a harness that keeps running; a stalled harness is
#144's warning, and stopping it stays with the operator.

Each launch, wait, exit, conversation ID and stop reason is appended to
``--sessions`` as one JSON line, without the token or any command line.
Ctrl-C or SIGTERM stops the current harness child, launches nothing more, and
exits 130. A long backoff wait is interruptible: Ctrl-C stops promptly.

``prepare-run.py`` writes the call into ``start-alice`` and ``resume-alice``.
It runs this file with the run's own interpreter, so the standard library only.
Everything after ``--`` is the harness command without its prompt or session
arguments, which this launcher adds::

    python scripts/alice-launcher.py --harness opencode \\
        --hub-url http://127.0.0.1:8420 --token-file /state/token \\
        --sessions RUN/alice-sessions.jsonl --prompt RUN/alice.prompt.md \\
        -- opencode run --auto --title 'alice my-run'

Exit codes: 0 done; 1 resumes spent (count or time budget); 2 usage;
3 hub unreadable or a different hub or workflow;
4 nothing safe to resume (no workflow, no conversation ID);
5 paused; 130 interrupted.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from agent_launcher import CONTINUE_PROMPT, main  # noqa: E402,F401

if __name__ == "__main__":
    sys.exit(main())
