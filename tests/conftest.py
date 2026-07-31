"""Shared test setup: isolate each test from the last one's state.

Tests drive the real CLI functions, which write to STATE_DIR and let commands
construct their own SSH. Both are reset here so a leaked session file or a
FakeSSH left behind by an earlier test cannot influence the next.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

import fakecluster as fc  # noqa: E402  (needs the path above)

cli = fc.load_cli()
fc.bind_errors(cli)


@pytest.fixture(autouse=True)
def clean_state():
    real_ssh = cli.SSH
    for p in cli.STATE_DIR.glob("*"):
        p.unlink()
    fc.FakeSSH.taken_ports = set()
    yield
    cli.SSH = real_ssh
    for p in cli.STATE_DIR.glob("*"):
        p.unlink()
