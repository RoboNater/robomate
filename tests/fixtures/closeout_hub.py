"""Pytest-only subprocess hub with a controlled two-clock advance endpoint."""

import os
import sys
from datetime import datetime
from pathlib import Path

import uvicorn
from agent_hub import create_app
from agent_hub_common import HubSettings

# tests/ is not installed; this fixture intentionally shares pytest's clock.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from timing import FrozenClock  # noqa: E402

state, port, start = sys.argv[1:]
url = f"http://127.0.0.1:{port}"
settings = HubSettings(
    host="127.0.0.1",
    port=int(port),
    public_url=url,
    state_dir=Path(state),
    database_path=Path(state) / "hub.db",
    token=os.environ["ROBOMATE_TOKEN"],
    token_file=Path(state) / "token",
    guides_dir=Path(__file__).resolve().parents[2] / "guides",
    sweep_interval_s=3600,
    lost_after_s=60,
    detect_host_pauses=False,
)
app = create_app(
    settings,
    hub_info={
        "hub_id": "pytest-closeout",
        "repo_root": state,
        "origin": "unknown",
        "forge": "unknown",
        "url": url,
        "default_branch": "main",
    },
)
clock = FrozenClock(datetime.fromisoformat(start))
store = app.state.store
clock.install(store)


@app.post("/test/advance")
def advance(body: dict[str, float]) -> dict[str, bool]:
    clock.advance(body["seconds"])
    # Only the killed worker goes silent. Preserve the living worker's liveness
    # before the first sweep; both clocks advance together.
    bob = store.agent_by_name("bob")
    if bob is not None:
        store.heartbeat("bob", bob.worker_instance_id, bob.current_task_id)
    store.sweep(settings.lost_after_s)
    return {"advanced": True}


if __name__ == "__main__":
    uvicorn.run(app, host=settings.host, port=settings.port)
