#!/usr/bin/env python3
"""Harness identity and version helpers shared by the joined launch scripts."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path, PurePath
from typing import Any

#: CLI invoked with ``--version`` to report each harness version.
VERSION_COMMANDS = {
    "claude-code": "claude",
    "codex": "codex",
    "opencode": "opencode",
    "antigravity": "agy",
    "devin": "devin",
    "gemini": "gemini",
}

#: Default model provider per harness where the harness implies the model maker.
PROVIDERS = {
    "claude-code": "anthropic",
    "codex": "openai",
    "gemini": "google",
}

#: Model-maker aliases in multi-vendor model IDs (e.g. OpenRouter or vendor prefixes).
MAKER_ALIASES = {
    "alibaba": "alibaba",
    "anthropic": "anthropic",
    "bytedance": "bytedance",
    "bytedance-seed": "bytedance",
    "cohere": "cohere",
    "deepseek": "deepseek",
    "google": "google",
    "meta": "meta",
    "meta-llama": "meta",
    "minimax": "minimax",
    "mistral": "mistral",
    "mistralai": "mistral",
    "moonshot": "moonshot",
    "moonshotai": "moonshot",
    "openai": "openai",
    "qwen": "alibaba",
    "x-ai": "xai",
    "xai": "xai",
    "z-ai": "zhipu",
    "zhipu": "zhipu",
}

#: OpenRouter namespace segments that name a routing gateway or undisclosed author, not a maker.
NON_MAKERS = {"openrouter", "stealth"}

#: Model family prefix -> model maker for bare model names and gateway routes (opencode/, agy).
MODEL_FAMILIES = (
    ("claude", "anthropic"),
    ("gpt", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("codex", "openai"),
    ("gemini", "google"),
    ("gemma", "google"),
    ("grok", "xai"),
    ("deepseek", "deepseek"),
    ("glm", "zhipu"),
    ("kimi", "moonshot"),
    ("minimax", "minimax"),
    ("qwen", "alibaba"),
    ("mistral", "mistral"),
    ("codestral", "mistral"),
    ("devstral", "mistral"),
    ("ministral", "mistral"),
    ("pixtral", "mistral"),
    ("llama", "meta"),
    ("muse-spark", "meta"),
    ("nemotron", "nvidia"),
    ("mimo", "xiaomi"),
    ("longcat", "meituan"),
    ("ling-", "inclusionai"),
)


def resolve_provider(harness: str, model: str = "", provider: str | None = None) -> str:
    """The organization that made ``model``, or ``unknown`` when not determinable.

    Explicit ``--<agent>-provider`` always wins. Otherwise the model ID is
    inspected first — taking ``<maker>`` from ``openrouter/<maker>/...`` (mapped
    through :data:`MAKER_ALIASES`, or ``unknown`` for :data:`NON_MAKERS`), then
    matching known model families on the model slug, then falling back to
    ``<maker>/<model>`` in :data:`MAKER_ALIASES` — so a non-default model on any
    harness (e.g. Claude Code routed to ``glm-5.3``, or ``alibaba/kimi-k2.5`` on
    OpenCode) records the model's maker rather than the harness vendor or
    hosting route. Single-vendor harnesses (``claude-code``, ``codex``,
    ``gemini``) then fall back to :data:`PROVIDERS`, and multi-vendor harnesses
    (``opencode``, ``antigravity``, ``devin``) fall back to ``unknown`` rather
    than guessing.
    """
    if provider:
        return provider
    text = model.strip().lower()
    if text:
        parts = [part.lstrip("~") for part in text.split("/") if part]
        if len(parts) >= 3 and parts[0] == "openrouter":
            if parts[1] in NON_MAKERS:
                return "unknown"
            return MAKER_ALIASES.get(parts[1], parts[1])
        slug = parts[-1]
        for prefix, maker in MODEL_FAMILIES:
            if slug == prefix or slug.startswith(prefix):
                return maker
        if len(parts) >= 2 and parts[0] in MAKER_ALIASES:
            return MAKER_ALIASES[parts[0]]
    if harness in PROVIDERS:
        return PROVIDERS[harness]
    return "unknown"


def bridge_identity_env(
    harness: str,
    version: str,
    provider: str,
    model: str,
    effort: str,
) -> dict[str, str]:
    """The declared identity every bridge (Alice or worker) carries (#402)."""
    return {
        "HUB_HARNESS_MODE": "print" if harness == "claude-code" else "headless",
        "HUB_HARNESS": harness,
        "HUB_HARNESS_VERSION": version,
        "HUB_PROVIDER": provider,
        "HUB_MODEL": model,
        "HUB_EFFORT": effort,
    }


def executable(name: str) -> str:
    """Resolve PATHEXT shims (npm .cmd, pyenv .bat) that CreateProcess cannot find alone."""
    return shutil.which(name) or name


def run(*args: Any, cwd: Path | str | None = None, env: dict[str, str] | None = None) -> str:
    """Run a subprocess, returning stripped stdout; raises on failure."""
    merged = None if env is None else {**os.environ, **env}
    return subprocess.run(
        [executable(str(args[0])), *[str(arg) for arg in args[1:]]],
        cwd=cwd,
        env=merged,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()


def agy_mcp_path[P: PurePath](home: P) -> P:
    """The ``mcp_config.json`` path inside an isolated AntiGravity home."""
    return home / ".gemini" / "config" / "mcp_config.json"


def parse_harness_version(harness: str, output: str) -> str:
    """The bare version reported at join, from the recorded ``--version`` output."""
    words = output.split()
    if not words:
        raise ValueError(f"{harness} reported an empty --version")
    if harness == "codex":
        return words[-1]
    # ``devin 3000.11.3 (9c803229faa4)``: the version follows the program name.
    if harness == "devin" and len(words) > 1 and words[0] == "devin":
        return words[1]
    # OpenCode 1.x prints ``1.19.5``; 2.x prints ``opencode v2.4.1``.
    if harness == "opencode":
        token = words[1] if len(words) > 1 and words[0].lower() == "opencode" else words[0]
        return token.lstrip("vV")
    return words[0]


def probe_harness_version(harness: str) -> tuple[str, str]:
    """Run the harness CLI's ``--version``; fail with an actionable message if missing."""
    if harness not in VERSION_COMMANDS:
        raise ValueError(f"unknown harness {harness!r}; expected one of {sorted(VERSION_COMMANDS)}")
    command = VERSION_COMMANDS[harness]
    try:
        output = run(command, "--version")
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            f"harness {harness!r} requires the {command!r} CLI on PATH "
            f"({command} --version failed); install and authenticate it first"
        ) from exc
    return output, parse_harness_version(harness, output)
