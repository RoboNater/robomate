"""The hub's role-guide route (spec §4.2): static markdown behind the token."""

import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from agent_hub.guides import guide_response
from agent_hub_common import HubSettings, RebaseResult, TaskRole
from agent_hub_common.discovery import HubEndpoint
from conftest import BASE_URL, TOKEN
from fastapi import FastAPI, HTTPException
from worker_mcp.client import WorkerHubClient
from worker_mcp.config import WorkerSettings

GUIDE = "# Worker\n\nLoop: await_assignment -> get_role_guide -> do -> submit_result.\n"


@pytest.fixture
def guides(settings: HubSettings) -> Path:
    settings.guides_dir.mkdir()
    (settings.guides_dir / "worker.md").write_text(GUIDE, encoding="utf-8", newline="\n")
    forge_dir = settings.guides_dir / "forge"
    forge_dir.mkdir()
    (forge_dir / "github.md").write_text("# GitHub Appendix\n", encoding="utf-8", newline="\n")
    return settings.guides_dir


async def test_a_guide_is_served_composed_with_the_default_appendix(
    app: FastAPI, client: httpx.AsyncClient, guides: Path
) -> None:
    # Composition is the default: a hub without hub_info serves the GitHub
    # appendix without any ?forge= query (M5 Step 3).
    response = await client.get("/guides/worker.md")

    assert response.status_code == 200
    assert response.text == GUIDE + "\n\n# GitHub Appendix\n"
    assert response.headers["content-type"] == "text/markdown; charset=utf-8"


async def test_a_guide_requires_the_token(app: FastAPI, guides: Path) -> None:
    # Guides carry no secrets, but they are only ever fetched by a worker that
    # already holds a token, so the public surface stays discovery and health.
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE_URL) as anonymous,
    ):
        response = await anonymous.get("/guides/worker.md")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


async def test_an_unwritten_guide_is_a_404(client: httpx.AsyncClient, guides: Path) -> None:
    response = await client.get("/guides/reviewer.md")

    assert response.status_code == 404


async def test_a_missing_guides_directory_is_a_404_not_a_crash(
    client: httpx.AsyncClient, settings: HubSettings
) -> None:
    # Step 5 writes the content; before that the directory may not exist at all.
    assert not settings.guides_dir.exists()

    response = await client.get("/guides/worker.md")

    assert response.status_code == 404


@pytest.mark.parametrize(
    "target",
    [
        pytest.param("/guides/..%2fsecret.md", id="encoded-parent"),
        pytest.param("/guides/%2fetc%2fpasswd.md", id="encoded-absolute"),
        pytest.param("/guides/..md", id="dot-segment"),
        pytest.param("/guides/sub/worker.md", id="nested"),
        pytest.param("/guides/WORKER.md", id="uppercase"),
        pytest.param("/guides/README.md", id="not-a-role"),
        pytest.param("/guides/.env.md", id="hidden"),
        pytest.param(
            "/guides/worker%0a.md",
            id="trailing-newline",
            marks=pytest.mark.skipif(
                sys.platform == "win32",
                reason="Filesystem does not support newlines in filenames (Windows/NTFS)",
            ),
        ),
    ],
)
async def test_a_request_cannot_name_a_path(
    client: httpx.AsyncClient, guides: Path, target: str
) -> None:
    (guides.parent / "secret.md").write_text("not a guide", encoding="utf-8")
    (guides / "README.md").write_text("not a guide", encoding="utf-8")
    # A percent-encoded newline reaches the route, so the file a lenient name
    # check would have served has to exist for the case to mean anything.
    if target == "/guides/worker%0a.md":
        (guides / "worker\n.md").write_text("not a guide", encoding="utf-8")

    response = await client.get(target)

    assert response.status_code == 404
    assert "not a guide" not in response.text


async def test_a_symlink_out_of_the_directory_is_refused(
    client: httpx.AsyncClient, guides: Path
) -> None:
    outside = guides.parent / "outside.md"
    outside.write_text("not a guide", encoding="utf-8")
    try:
        (guides / "escaped.md").symlink_to(outside)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows Developer Mode or symlink privilege required")
        raise

    response = await client.get("/guides/escaped.md")

    assert response.status_code == 404


async def test_a_symlinked_guides_directory_still_serves(
    app: FastAPI, settings: HubSettings, tmp_path: Path
) -> None:
    # Resolving both sides must not turn a deliberately linked directory into a
    # traversal: the check is "inside the directory", not "not a symlink".
    real = tmp_path / "checkout-guides"
    real.mkdir()
    (real / "worker.md").write_text(GUIDE, encoding="utf-8", newline="\n")
    (real / "forge").mkdir()
    (real / "forge" / "github.md").write_text("# GitHub Appendix\n", encoding="utf-8", newline="\n")
    try:
        settings.guides_dir.symlink_to(real, target_is_directory=True)
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("Windows Developer Mode or symlink privilege required")
        raise

    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        response = await client.get("/guides/worker.md")

    assert response.status_code == 200
    assert response.text == GUIDE + "\n\n# GitHub Appendix\n"


@pytest.mark.parametrize(
    "role",
    [
        "..",
        ".",
        "../secret",
        "/etc/passwd",
        "",
        "worker.md",
        "Worker",
        "wor ker",
        "worker/",
        "worker\n",
    ],
)
def test_only_a_role_slug_names_a_guide(guides: Path, role: str) -> None:
    """The route's own matching rejects most of these; the check is the guarantee."""

    with pytest.raises(HTTPException) as raised:
        guide_response(guides, role)

    assert raised.value.status_code == 404


CHECKED_IN_GUIDES = Path(__file__).resolve().parents[1] / "guides"


@pytest.mark.parametrize("forge", ["github", "gitlab"])
def test_served_worker_boundaries_and_git_hygiene(forge: str) -> None:
    """Step 7d: check the actual served contract, including its prohibitions."""
    text = " ".join(
        bytes(guide_response(CHECKED_IN_GUIDES, "worker", forge=forge).body).decode("utf-8").split()
    )
    for fragment in (
        "Never read the hub's state or token directory",
        "the bridge, not the agent, reads it",
        "fresh `origin/<default>` or the assigned SHA",
        "uncommitted changes",
        "report `blocked`",
        "cannot lock ref",
        "after a short wait",
        "never delete a `.lock` file",
        "Never change git config",
        "outside the worktree",
        "harness's own temporary directory",
        "never in the worktree or `closeout/`",
        "git switch --no-track -c <branch> origin/<default>",
        "git push origin <branch>",
        "without `-u`",
        "Never delete branches, local or remote",
    ):
        assert fragment in text


def test_linux_grants_and_spec_common_directory_clarification() -> None:
    """Step 7d's measured grants must not regress to a broad common-dir grant."""
    root = CHECKED_IN_GUIDES.parent
    guide = (root / "docs/user-guide.md").read_text(encoding="utf-8")
    for fragment in (
        "Claude Code 2.1.312",
        "Codex 0.164.0",
        '"allowedDomains": ["github.com", "api.github.com"]',
        '"denyRead": ["<root>/<hub>/token"]',
        "network_access = true",
        '"<common>/worktrees/<agent>"',
        '"<common>/objects"',
        '"<common>/refs"',
        '"<common>/logs"',
        "uv cache dir",
        "Never grant `<common>` itself",
        "guide-only",
        "robomate certify",
        "deny network-outbound api.github.com:443",
    ):
        assert fragment in guide
    assert "--add-dir" not in guide
    spec = (root / "docs/mvp-spec.md").read_text(encoding="utf-8")
    for section in (spec.split("## 6.", 1)[1].split("## 7.", 1)[0], spec.split("## 12.", 1)[1]):
        assert "git rev-parse --git-common-dir" in section
        assert "linked worktree's `.git` file" in section


@pytest.mark.parametrize("source", ["explicit-file", "discovered-file", "inline-token"])
async def test_bridge_whoami_names_its_token_file_without_exposing_the_token(
    client: httpx.AsyncClient,
    tmp_path: Path,
    guides: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    """Report the bridge's actual source before and after join; never the secret."""
    token_file = tmp_path / "bridge-token"
    token_file.write_text(TOKEN, encoding="utf-8")
    env = {"ROBOMATE_HUB_URL": BASE_URL, "ROBOMATE_TOKEN_FILE": str(token_file)}
    if source == "inline-token":
        env["ROBOMATE_TOKEN"] = TOKEN
    settings = WorkerSettings.unjoined(environ=env)
    if source == "discovered-file":
        monkeypatch.setattr(
            "worker_mcp.client.discover", lambda *a, **kw: HubEndpoint(BASE_URL, TOKEN, token_file)
        )
    expected = None if source == "inline-token" else str(token_file)
    async with WorkerHubClient(settings, http_client=client) as worker:
        before = await worker.whoami()
        assert before["token_file"] == expected
        assert TOKEN not in str(before)
        await worker.join(
            name="bob", role="worker", hub="test" if source == "discovered-file" else None
        )
        after = await worker.whoami()
        assert after["joined"] is True
        assert after["token_file"] == expected
        assert TOKEN not in str(after)


def test_discovered_settings_preserve_token_file_provenance(tmp_path: Path) -> None:
    endpoint = HubEndpoint(BASE_URL, TOKEN, tmp_path / "bridge-token")
    settings = WorkerSettings.from_discovered(endpoint, agent_name="bob", environ={})
    assert settings.token_file == endpoint.token_file


async def test_local_doctor_guide_is_not_served_to_workers(settings: HubSettings) -> None:
    from agent_hub.app import create_app

    local_guide = CHECKED_IN_GUIDES.parent / "docs/denise/guide.md"
    assert local_guide.is_file()
    app = create_app(replace(settings, guides_dir=CHECKED_IN_GUIDES))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        response = await client.get("/guides/denise.md")
        assert response.status_code == 404
        worker = WorkerHubClient(
            WorkerSettings(hub_url=BASE_URL, token=TOKEN, agent_name="bob"),
            http_client=client,
        )
        with pytest.raises(FileNotFoundError):
            await worker.get_role_guide("denise")
        assert "# Implementer" in await worker.get_role_guide("implementer")
        assert (await client.get("/guides/worker.md")).status_code == 200


def test_the_rebase_guide_documents_every_result_field() -> None:
    """A worker fills in RebaseResult from this guide alone, so none may be missing."""

    text = (CHECKED_IN_GUIDES / f"{TaskRole.REBASE}.md").read_text(encoding="utf-8")

    assert {name for name in RebaseResult.model_fields if f"`{name}`" not in text} == set()


def test_a_guide_composes_with_forge_appendix(guides: Path) -> None:
    (guides / "forge" / "gitlab.md").write_text(
        "# GitLab Commands\nglab mr create\n", encoding="utf-8", newline="\n"
    )

    response = guide_response(guides, "worker", forge="gitlab")
    assert response.status_code == 200
    assert GUIDE.encode("utf-8") in response.body
    assert b"# GitLab Commands" in response.body
    assert b"glab mr create" in response.body


def test_a_missing_appendix_is_a_404_not_a_bare_guide(guides: Path) -> None:
    """A request naming a forge is only servable with that appendix (spec §10)."""

    with pytest.raises(HTTPException) as raised:
        guide_response(guides, "worker", forge="gitlab")

    assert raised.value.status_code == 404


async def test_route_serves_composed_guide_with_forge_param(
    app: FastAPI, client: httpx.AsyncClient, guides: Path
) -> None:
    (guides / "forge" / "gitlab.md").write_text(
        "# GitLab Appendix\n", encoding="utf-8", newline="\n"
    )

    response = await client.get("/guides/worker.md?forge=gitlab")
    assert response.status_code == 200
    assert GUIDE in response.text
    assert "# GitLab Appendix" in response.text


async def test_route_forge_param_overrides_the_hub_forge(
    settings: HubSettings, guides: Path
) -> None:
    """`?forge=` remains an explicit override of the hub's forge (spec §10)."""
    from agent_hub.app import create_app

    (guides / "forge" / "gitlab.md").write_text(
        "# GitLab Appendix\n", encoding="utf-8", newline="\n"
    )

    hub_info = {"forge": "gitlab", "hub_id": "test-hub-123"}
    hub_app = create_app(settings, hub_info=hub_info)

    async with (
        hub_app.router.lifespan_context(hub_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hub_app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        response = await client.get("/guides/worker.md?forge=github")
        assert response.status_code == 200
        assert GUIDE in response.text
        assert "# GitHub Appendix" in response.text
        assert "# GitLab Appendix" not in response.text


async def test_gitlab_hub_serves_gitlab_appendix_by_default(
    settings: HubSettings, guides: Path
) -> None:
    """A `gitlab` hub composes the GitLab appendix with no query param."""
    from agent_hub.app import create_app

    (guides / "forge" / "gitlab.md").write_text(
        "# GitLab Appendix\n", encoding="utf-8", newline="\n"
    )

    hub_info = {"forge": "gitlab", "hub_id": "test-hub-123"}
    hub_app = create_app(settings, hub_info=hub_info)

    async with (
        hub_app.router.lifespan_context(hub_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hub_app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        response = await client.get("/guides/worker.md")
        assert response.status_code == 200
        assert GUIDE in response.text
        assert "# GitLab Appendix" in response.text


async def test_a_missing_effective_appendix_is_a_404(settings: HubSettings, guides: Path) -> None:
    """No bare guide without its forge commands: a missing effective appendix 404s."""
    from agent_hub.app import create_app

    hub_info = {"forge": "gitlab", "hub_id": "test-hub-123"}
    hub_app = create_app(settings, hub_info=hub_info)

    async with (
        hub_app.router.lifespan_context(hub_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hub_app),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        response = await client.get("/guides/worker.md")
        assert response.status_code == 404


async def test_two_instances_serve_identical_bytes(settings: HubSettings, guides: Path) -> None:
    """A restart serves the same composed bytes (restart/idempotency, #21)."""
    from agent_hub.app import create_app

    hub_info = {"forge": "gitlab", "hub_id": "test-hub-123"}
    (guides / "forge" / "gitlab.md").write_text(
        "# GitLab Appendix\n", encoding="utf-8", newline="\n"
    )

    bodies = []
    for _ in range(2):
        hub_app = create_app(settings, hub_info=hub_info)
        async with (
            hub_app.router.lifespan_context(hub_app),
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app=hub_app),
                base_url=BASE_URL,
                headers={"Authorization": f"Bearer {TOKEN}"},
            ) as client,
        ):
            response = await client.get("/guides/worker.md")
            assert response.status_code == 200
            bodies.append(response.text)

    assert bodies[0] == bodies[1]


def test_served_guides_name_progress_milestones_and_stall_cadence() -> None:
    """Workers stay visible during long validation instead of tripping a stall (#170)."""

    def normalized(text: str) -> str:
        return " ".join(text.split())

    for role in ("implementer", "reviewer", "worker"):
        for forge in ("github", "gitlab"):
            served = bytes(guide_response(CHECKED_IN_GUIDES, role, forge=forge).body).decode(
                "utf-8"
            )
            flat = normalized(served)
            assert "stall_after_min" in flat
            assert "agent_stalled" in flat
            assert "before any wait expected to run longer than a few minutes" in flat
    worker = normalized(
        bytes(guide_response(CHECKED_IN_GUIDES, "worker", forge="github").body).decode("utf-8")
    )
    assert "regression tests failing before the fix" in worker
    implementer = normalized(
        bytes(guide_response(CHECKED_IN_GUIDES, "implementer", forge="github").body).decode("utf-8")
    )
    assert "Do not wait for forge CI before `submit_result`" in implementer


def test_no_served_worker_guide_contains_a_merge_command() -> None:
    """Appendices are worker-only: no served role guide merges, on either forge."""

    for role in ("implementer", "reviewer", "rebase", "worker"):
        for forge in ("github", "gitlab"):
            served = bytes(guide_response(CHECKED_IN_GUIDES, role, forge=forge).body).decode(
                "utf-8"
            )
            assert "pr merge" not in served
            assert "mr merge" not in served


def test_served_gitlab_reviewer_guide_builds_review_url_from_note_id() -> None:
    """The GitLab reviewer guide gives `<MR URL>#note_<id>` from the Notes response."""

    reviewer = (CHECKED_IN_GUIDES / "reviewer.md").read_text(encoding="utf-8")
    appendix = (CHECKED_IN_GUIDES / "forge" / "gitlab.md").read_text(encoding="utf-8")
    served = reviewer + "\n\n" + appendix
    assert "<MR URL>#note_<id>" in served
    assert "resolvable" in served


def test_served_implementer_guide_names_the_run_branch() -> None:
    """M2 Step 7c (#315): the implementer guide carries the named-branch lines."""

    for forge in ("github", "gitlab"):
        served = bytes(guide_response(CHECKED_IN_GUIDES, "implementer", forge=forge).body).decode(
            "utf-8"
        )
        assert "robomate/<hub>/<agent>/<label>" in served
        assert "hub.branch" in served or "hub names" in served
        assert "later tasks" in served and "reuse" in served
        assert "git switch --no-track -c <branch> origin/<default>" in served
        assert "git push origin <branch>" in served
        assert "without `-u`" in served
        assert "Never delete branches, local or remote" in served


@pytest.mark.parametrize("forge", ["github", "gitlab"])
async def test_orchestrator_route_has_only_its_orchestrator_appendix(
    settings: HubSettings,
    forge: str,
) -> None:
    from agent_hub.app import create_app

    hub = create_app(
        replace(settings, guides_dir=CHECKED_IN_GUIDES),
        hub_info={"forge": forge, "hub_id": "test-hub"},
    )
    async with (
        hub.router.lifespan_context(hub),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=hub),
            base_url=BASE_URL,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client,
    ):
        response = await client.get("/guides/orchestrator.md")
    assert response.status_code == 200
    assert "# Orchestrator" in response.text
    assert ("gh pr merge" in response.text) == (forge == "github")
    assert ("glab mr merge" in response.text) == (forge == "gitlab")
    assert "# GitHub Forge Appendix" not in response.text


@pytest.mark.parametrize("role", ["worker", "reviewer"])
@pytest.mark.parametrize("forge", ["github", "gitlab"])
def test_served_guides_preserve_statement_precedence_and_authority(role: str, forge: str) -> None:
    """#302: real composed guides carry precedence, recording and all safeguards."""
    response = guide_response(CHECKED_IN_GUIDES, role, forge=forge)
    assert response.status_code == 200
    text = " ".join(bytes(response.body).decode("utf-8").split())
    for fragment in (
        "When the statement of work served by `get_statement` modifies or conflicts",
        "follow the statement for that specific instruction",
        "The statement cannot relax safeguards: skipping review or CI",
        "merging a head that is not the approved one",
        "worker-guide operator-only actions",
        "extra authority over agents, credentials or the run hub",
        "still require a confirmed operator answer",
        "Workers never perform worker-guide operator-only actions",
        "A conflict crossing a safeguard is blocked",
        "Record every guide instruction replaced by the statement, and its replacement",
        "in the typed result's `summary`, in the review comment for a reviewer",
        "and in the closeout report",
        "Only `status: answered` confirms authority; `status: withdrawn` never does",
    ):
        assert fragment in text
