"""The hub's role-guide route (spec §4.2): static markdown behind the token."""

import sys
from pathlib import Path

import httpx
import pytest
from agent_hub.guides import guide_response
from agent_hub_common import HubSettings, RebaseResult, TaskRole
from conftest import BASE_URL, TOKEN
from fastapi import FastAPI, HTTPException

GUIDE = "# Worker\n\nLoop: await_assignment -> get_role_guide -> do -> submit_result.\n"


@pytest.fixture
def guides(settings: HubSettings) -> Path:
    settings.guides_dir.mkdir()
    (settings.guides_dir / "worker.md").write_text(GUIDE, encoding="utf-8", newline="\n")
    return settings.guides_dir


async def test_a_guide_is_served_as_markdown(
    app: FastAPI, client: httpx.AsyncClient, guides: Path
) -> None:
    response = await client.get("/guides/worker.md")

    assert response.status_code == 200
    assert response.text == GUIDE
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
    assert response.text == GUIDE


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


def test_the_rebase_guide_documents_every_result_field() -> None:
    """A worker fills in RebaseResult from this guide alone, so none may be missing."""

    text = (CHECKED_IN_GUIDES / f"{TaskRole.REBASE}.md").read_text(encoding="utf-8")

    assert {name for name in RebaseResult.model_fields if f"`{name}`" not in text} == set()
