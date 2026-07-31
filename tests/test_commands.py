"""Command-level behaviour: what the subcommands do with the state they synced.

The recurring failure here is a command syncing one collection and then acting
on a different one (local files vs cluster files vs the synced view).
"""
from __future__ import annotations

import pytest

import fakecluster as fc

cli = fc.load_cli()
fc.bind_errors(cli)

JOB, NODE = "555", "c01n02"



def cfg():
    return cli.Config(cli.DEFAULTS | {"host": "fake"})


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_down_all_tears_down_a_cluster_only_session():
    """`down --all` synced the cluster but iterated Sessions.list_names(), so a
    session with no local file was skipped: its job kept running, its cluster
    file stayed, and the command reported success."""
    ssh = fc.FakeSSH(files={f"session.remote-only@{JOB}": fc.session_file(job=JOB)},
                     jobs={JOB: fc.running()})
    fc.install(cli, ssh)

    cli.cmd_down(Args(all=True, name=None), cfg())

    assert ssh.scancelled == [JOB]
    assert ssh.files == {}
    assert list(cli.STATE_DIR.glob("session.*")) == []


def test_down_all_tears_down_an_unverifiable_cluster_session():
    """The regression `down --all` actually had: a session the sync could not
    confirm is never written to local state, so iterating local files skipped
    it and left both the cluster file and the job behind."""
    ghost = "777"
    ssh = fc.FakeSSH(files={f"session.ghost@{ghost}": fc.session_file(job=ghost)},
                     sacct={ghost: ""})           # not queued, accounting silent
    fc.install(cli, ssh)
    assert cli.sync_all(ssh, cli.Slurm(ssh), cfg())["ghost"].status == "stale_unverified"
    assert list(cli.STATE_DIR.glob("session.*")) == [], "nothing local to enumerate"

    cli.cmd_down(Args(all=True, name=None), cfg())

    assert ssh.files == {}
    assert ssh.scancelled == [ghost]


def test_down_all_reports_nothing_when_there_is_nothing():
    ssh = fc.FakeSSH()
    fc.install(cli, ssh)

    cli.cmd_down(Args(all=True, name=None), cfg())

    assert ssh.scancelled == []


def test_down_named_scancels_the_job_the_cluster_reports():
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(job=JOB)},
                     jobs={JOB: fc.running()})
    fc.install(cli, ssh)

    cli.cmd_down(Args(all=False, name="default"), cfg())

    assert ssh.scancelled == [JOB]


def test_adopt_picks_up_a_running_session_in_one_pass():
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(NODE, "46337", "tok", JOB)},
                     jobs={JOB: fc.running(NODE)})
    fc.install(cli, ssh)

    cli.cmd_adopt(Args(name=None), cfg())

    assert ssh.forwards and ssh.forwards[0].endswith(f":{NODE}:46337")
    assert cli.Sessions.load("default")["JOB_ID"] == JOB


def test_url_refuses_a_tunnel_pointing_at_the_old_node(monkeypatch):
    """The spec was compared on local port alone, so a session that moved nodes
    printed a URL into a tunnel that went nowhere."""
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888",
                                  "NODE": "newnode", "PORT": "2222", "TOKEN": "t"})
    cli.atomic_write_text(cli.Sessions.tunnel_path("default"), "8888:oldnode:1111\n")
    view = cli.SessionView(name="default", status="live",
                           payload=cli.Sessions.load("default"),
                           job_state="RUNNING", age_s=0)
    monkeypatch.setattr(cli, "sync_one", lambda *a, **k: view)

    with pytest.raises(SystemExit):
        cli.cmd_url(Args(name="default"), cfg())

    cli.atomic_write_text(cli.Sessions.tunnel_path("default"), "8888:newnode:2222\n")
    cli.cmd_url(Args(name="default"), cfg())      # matching spec is accepted


def test_forward_up_records_only_what_it_opened():
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888",
                                  "NODE": NODE, "PORT": "46337", "TOKEN": "t"})
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(NODE, "46337", "t", JOB)},
                     jobs={JOB: fc.running(NODE)})
    fc.install(cli, ssh)
    fc.FakeSSH.taken_ports = {9002}

    with pytest.raises(SystemExit):
        cli.cmd_forward_up(Args(ports=["9001", "9002"], remote=None, node=None, name=None), cfg())

    assert cli.parse_forwards(cli.Sessions.load("default")["FORWARDS"]) == [(9001, 9001)]
    assert ssh.forwards == [f"9001:{NODE}:9001"]
