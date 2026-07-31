"""A fake Bouchet, so the session-state logic can be tested without ssh.

`FakeSSH` answers the shell snippets bouchet-jupyter actually sends (the session
file listing, squeue, sacct, scancel, rm) out of in-memory dicts, and records
what it was asked to do. Tests drive the real functions against it.

Nothing here touches the network, the user's ~/.local/state, or the cluster:
`load_cli()` points XDG_STATE_HOME at a temp dir per test module.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import re
import sys
import tempfile
from pathlib import Path

CLI = Path(__file__).resolve().parent.parent / "bouchet-jupyter"


MODULE = "bouchet_jupyter_cli"


def load_cli(state_dir: str | None = None):
    """Import the CLI (which has no .py suffix) as a module, with its local
    state redirected to a scratch directory.

    Memoized: every test file must share one module object, or each would get
    its own SSHError class and `except SSHError` would stop matching across
    files. STATE_DIR is per-run and the tests clear it between cases."""
    if MODULE in sys.modules:
        return sys.modules[MODULE]
    os.environ["XDG_STATE_HOME"] = state_dir or tempfile.mkdtemp()
    loader = importlib.machinery.SourceFileLoader(MODULE, str(CLI))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[loader.name] = mod
    loader.exec_module(mod)
    return mod


class FakeSSH:
    """Stands in for SSH. `files` maps a remote basename to its contents;
    `jobs` maps a job id to a squeue row 'STATE|REASON|ELAPSED|LIMIT|NODE'."""

    def __init__(self, files: dict[str, str] | None = None,
                 jobs: dict[str, str] | None = None, host: str = "fake",
                 sacct: dict[str, str] | None = None):
        self.host = host
        self.files = dict(files or {})
        self.jobs = dict(jobs or {})
        # Per-job sacct answers. Default: queued jobs are RUNNING, anything else
        # COMPLETED. Override with "" for the accounting-lag case, where sacct
        # can neither confirm nor deny that a job has ended.
        self.sacct = dict(sacct or {})
        self.forwards: list[str] = []
        self.cancelled: list[str] = []
        self.commands: list[str] = []
        self.scancelled: list[str] = []
        self.fail = False           # set True to simulate the cluster being unreachable

    # -- the bits the CLI calls -------------------------------------------
    def run(self, cmd, *, check=True, timeout=None, input_data=None, retry=True):
        self.commands.append(cmd)
        if self.fail:
            raise self.SSHError("ssh: connection refused")
        out: list[str] = []
        for part in cmd.split(" ; "):
            out.extend(self.run_fragment(part.strip()))
        return "\n".join(out)

    def forward(self, spec):
        lport = int(spec.split(":")[0])
        if lport in self.taken_ports:
            raise self.SSHError("tunnel forward failed: mux_client_forward: "
                                "forwarding request failed: Port forwarding failed")
        self.forwards.append(spec)

    def cancel_forward(self, spec):
        self.cancelled.append(spec)

    taken_ports: set[int] = set()
    SSHError: type = RuntimeError     # replaced by bind_errors() below

    # -- shell emulation ---------------------------------------------------
    def run_fragment(self, part: str) -> list[str]:
        if part.startswith("echo "):
            # `echo "==FILE==$(basename "$f")"` belongs to the listing loop,
            # which list_session_files() already expands.
            return [] if "$(" in part else [part[5:]]
        if part.startswith("for f in") and "session." in part:
            return self.list_session_files(part)
        if part.startswith("squeue --me"):
            return [f"{jid}|{row}" for jid, row in self.jobs.items()]
        if part.startswith("squeue"):
            jid = self.arg_after(part, "-j")
            return [self.jobs[jid]] if jid in self.jobs else []
        if part.startswith("sacct"):
            jid = self.arg_after(part, "-j")
            if jid in self.sacct:
                return [self.sacct[jid]]
            return ["RUNNING" if jid in self.jobs else "COMPLETED"]
        if part.startswith("scancel"):
            jid = part.split()[1]
            self.scancelled.append(jid)
            self.jobs.pop(jid, None)
            return []
        if part.startswith("rm -f"):
            self.remove(part)
            return []
        return []

    def list_session_files(self, part: str) -> list[str]:
        """Emulate `for f in <patterns> ; do echo ==FILE==base ; cat "$f" ; done`.
        There may be several patterns (per-job files plus the legacy path)."""
        body = part.split("for f in", 1)[1].split(";", 1)[0]
        out: list[str] = []
        for base in self.match_patterns(body):
            out.append(f"==FILE=={base}")
            out.append(self.files[base].rstrip("\n"))
        return out

    def remove(self, part: str) -> None:
        for base in self.match_patterns(part.split("rm -f", 1)[1]):
            del self.files[base]

    def match_patterns(self, blob: str) -> list[str]:
        """Basenames matching any shell pattern in `blob` (only trailing '*' is
        supported, which is all the CLI uses), in stable order."""
        hits: list[str] = []
        for token in blob.split():
            base = token.rsplit("/", 1)[-1].strip("'\"")
            for candidate in sorted(self.files):
                if candidate in hits:
                    continue
                if candidate == base or (base.endswith("*")
                                         and candidate.startswith(base[:-1])):
                    hits.append(candidate)
        return hits

    @staticmethod
    def arg_after(part: str, flag: str) -> str:
        toks = part.split()
        return toks[toks.index(flag) + 1] if flag in toks else ""


def bind_errors(cli) -> None:
    """Point FakeSSH at the CLI's own SSHError so `except SSHError` catches it."""
    FakeSSH.SSHError = cli.SSHError


def session_file(node="c01n02", port="46337", token="tok", job="555") -> str:
    return f"NODE={node}\nPORT={port}\nTOKEN={token}\nJOB_ID={job}\n"


def running(node="c01n02", elapsed="00:05:00", limit="08:00:00") -> str:
    return f"RUNNING|{node}|{elapsed}|{limit}|{node}"


def install(cli, ssh) -> None:
    """Make the CLI's command functions use this FakeSSH instead of dialling out."""
    cli.SSH = lambda host: ssh
