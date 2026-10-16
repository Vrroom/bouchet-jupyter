"""What the CLI assumes about the ssh layer.

Both bugs here are the same shape as the local-port one: a probe that answers a
subtly different question than the one being asked, and a silent fallback that
made the answer look authoritative.
"""
from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone

import fakecluster as fc

cli = fc.load_cli()
fc.bind_errors(cli)

JOB = "21185944"


def cfg():
    return cli.Config(cli.DEFAULTS | {"host": "fake"})


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeProc:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


TRANSIENT = "Connection closed by remote host"


def fake_ssh_binary(monkeypatch, handler):
    """Replace the ssh subprocess itself, so SSH.run's own retry loop runs.
    A fake that overrides SSH.run would skip the very logic under test."""
    calls: list[str] = []

    def fake_run(argv, **kw):
        calls.append(argv[-1])
        return handler(argv[-1])

    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    return calls


def test_run_retries_transient_failures_but_only_when_asked(monkeypatch):
    calls = fake_ssh_binary(monkeypatch, lambda cmd: FakeProc(255, stderr=TRANSIENT))

    with pytest.raises(cli.SSHError):
        cli.SSH("h").run("squeue")
    assert len(calls) == 2, "a dropped connection is worth one retry"

    calls.clear()
    with pytest.raises(cli.SSHError):
        cli.SSH("h").run("squeue", retry=False)
    assert len(calls) == 1


def test_submit_is_never_retried_and_recovers_the_queued_job(monkeypatch):
    """sbatch is not idempotent: retrying it after the reply is lost queues a
    second job that nothing tracks, holding a GPU for its whole walltime."""
    marker = {"value": ""}

    def handler(cmd):
        if "sbatch" in cmd:
            marker["value"] = JOB            # the job really was queued...
            return FakeProc(255, stderr=TRANSIENT)   # ...and then the reply was lost
        if "last-submit" in cmd:
            return FakeProc(0, stdout=marker["value"])
        return FakeProc(0, stdout="")

    calls = fake_ssh_binary(monkeypatch, handler)

    job_id = cli.Slurm(cli.SSH("h")).submit("/s", "j.sh", [], "default", "/state")

    assert sum("sbatch" in c for c in calls) == 1, "must not submit twice"
    assert job_id == JOB, "and must adopt the job that did get queued"


def test_submit_does_not_adopt_an_earlier_submissions_job_id(monkeypatch):
    """The marker holds the previous job id until the submit command clears it.
    A connection that dies before that must not report the old job as the new
    one, or `up` would tunnel to a session it never started."""
    def handler(cmd):
        if "sbatch" in cmd:
            return FakeProc(255, stderr=TRANSIENT)   # died before clearing it
        if "last-submit" in cmd:
            return FakeProc(0, stdout="999")         # left by an earlier `up`
        return FakeProc(0, stdout="")

    fake_ssh_binary(monkeypatch, handler)

    with pytest.raises(cli.SSHError):
        cli.Slurm(cli.SSH("h")).submit("/s", "j.sh", [], "default", "/state")


def test_unreachable_cluster_is_not_read_as_an_empty_cluster():
    """SSH.run swallows failures when check=False, so probing that way made a
    dead connection look like a cluster with no jobs and no session files."""
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888",
                                  "NODE": "n", "PORT": "1", "TOKEN": "t"})
    ssh = fc.FakeSSH()
    ssh.fail = True

    view = cli.sync_one("default", ssh, cli.Slurm(ssh), cfg(), max_age_s=0)

    assert view.status == "unknown"
    assert cli.Sessions.load("default"), "state we cannot verify is state we keep"


def test_down_all_refuses_when_the_cluster_is_unreachable():
    """It would delete local state and print "scancelled" for jobs that are
    still running, because scancel fails silently."""
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888"})
    ssh = fc.FakeSSH()
    ssh.fail = True
    fc.install(cli, ssh)

    with pytest.raises(SystemExit):
        cli.cmd_down(Args(all=True, name=None), cfg())

    assert ssh.scancelled == []
    assert cli.Sessions.load("default"), "local state must survive"


def test_cluster_utc_offset_is_read_from_the_cluster(monkeypatch):
    ssh = fc.FakeSSH()
    monkeypatch.setattr(ssh, "run", lambda cmd, **kw: "-0400" if "date" in cmd else "")
    assert cli.cluster_utc_offset(ssh) == timezone(-timedelta(hours=4))


def test_eta_uses_the_clusters_timezone_not_the_laptops():
    """squeue prints start times in cluster-local time with no offset. Reading
    them against the laptop's clock is right only while the two timezones
    happen to agree."""
    here = datetime.now()
    start = (here + timedelta(hours=3)).replace(microsecond=0).isoformat()

    assert cli.fmt_eta(start).startswith("in 2h"), "same zone: three hours out"

    an_hour_ahead = timezone(here.astimezone().utcoffset() + timedelta(hours=1))
    assert cli.fmt_eta(start, an_hour_ahead).startswith("in 1h"), \
        "that same wall clock on a cluster an hour ahead is two hours out"


def test_eta_survives_a_cluster_that_will_not_say(monkeypatch):
    ssh = fc.FakeSSH()
    monkeypatch.setattr(ssh, "run", lambda cmd, **kw: "")
    assert cli.cluster_utc_offset(ssh) is None
    assert cli.fmt_eta("N/A", None) == "-"
