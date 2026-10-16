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


def two_sessions(other="gpu-big", other_job="666"):
    """Two live sessions, both tracked locally — the case where a bare `down`
    has to guess which job the user meant."""
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(job=JOB),
                            f"session.{other}@{other_job}": fc.session_file(job=other_job)},
                     jobs={JOB: fc.running(), other_job: fc.running()})
    fc.install(cli, ssh)
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888"})
    cli.Sessions.save(other, {"JOB_ID": other_job, "LOCAL_PORT": "8889"})
    return ssh


def answer(monkeypatch, *replies):
    """Drive prompt_for_choice: interactive, with `replies` typed in turn."""
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    pending = list(replies)
    monkeypatch.setattr("builtins.input", lambda *a: pending.pop(0))


def test_bare_down_asks_which_session_when_several_are_tracked(monkeypatch):
    """Bare `down` used to assume 'default' — with two sessions up that is a
    coin flip that scancels the wrong job."""
    ssh = two_sessions()
    answer(monkeypatch, "2")            # rows are sorted: 1) default, 2) gpu-big

    cli.cmd_down(Args(all=False, name=None), cfg())

    assert ssh.scancelled == ["666"]
    assert cli.Sessions.load("default")["JOB_ID"] == JOB, "the other one survives"


def test_bare_down_accepts_the_session_name_as_the_answer(monkeypatch):
    ssh = two_sessions()
    answer(monkeypatch, "nope", "gpu-big")   # a bad answer re-asks

    cli.cmd_down(Args(all=False, name=None), cfg())

    assert ssh.scancelled == ["666"]


def test_bare_down_cancels_without_touching_anything(monkeypatch):
    ssh = two_sessions()
    answer(monkeypatch, "q")

    cli.cmd_down(Args(all=False, name=None), cfg())

    assert ssh.scancelled == []
    assert sorted(p.name for p in cli.STATE_DIR.glob("session.*")) == \
        ["session.default", "session.gpu-big"]


def test_bare_down_with_no_tty_still_means_default(monkeypatch):
    """Scripts and pipes cannot answer a prompt, so the old default has to
    hold there — blocking on input would hang them."""
    ssh = two_sessions()
    monkeypatch.setattr(cli, "is_interactive", lambda: False)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted with no tty"))

    cli.cmd_down(Args(all=False, name=None), cfg())

    assert ssh.scancelled == [JOB]


def test_bare_down_does_not_ask_about_a_single_session(monkeypatch):
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(job=JOB)},
                     jobs={JOB: fc.running()})
    fc.install(cli, ssh)
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted for one session"))

    cli.cmd_down(Args(all=False, name=None), cfg())

    assert ssh.scancelled == [JOB]


def live_plus_dead(dead="job2", dead_job="23710782"):
    """One running session plus a local file left behind by an ended job -
    the state every synced command cleans up and every unsynced one trips on."""
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(NODE, "51577", "t", JOB)},
                     jobs={JOB: fc.running(NODE)})
    fc.install(cli, ssh)
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888",
                                  "NODE": NODE, "PORT": "51577", "TOKEN": "t"})
    cli.Sessions.save(dead, {"JOB_ID": dead_job, "LOCAL_PORT": "8889"})
    return ssh


def test_forward_up_is_not_blocked_by_a_dead_session_file():
    """`forward up` counted local files, so the file of a job that had already
    ended made the one live session look ambiguous and the command refused."""
    ssh = live_plus_dead()

    cli.cmd_forward_up(Args(ports=["8765"], remote=None, node=None, name=None), cfg())

    assert ssh.forwards == [f"8765:{NODE}:8765"]
    assert cli.Sessions.list_names() == ["default"], "the dead file is cleaned"


def test_name_resolution_still_refuses_two_live_sessions():
    ssh = two_sessions()

    with pytest.raises(SystemExit):
        cli.cmd_forward_up(Args(ports=["8765"], remote=None, node=None, name=None), cfg())

    assert ssh.forwards == []


def test_tunnel_up_accepts_a_port_held_only_by_a_dead_session():
    """`--port` was checked against every local file, so a port whose only
    claimant was an ended job's leftover state was refused outright."""
    ssh = live_plus_dead()

    cli.cmd_tunnel_up(Args(name="default", port=8889), cfg())

    assert ssh.forwards == [f"8889:{NODE}:51577"]
    assert cli.Sessions.load("default")["LOCAL_PORT"] == "8889"


def test_tunnel_up_still_refuses_a_live_session_port():
    ssh = two_sessions()
    ssh.files["session.gpu-big@666"] = fc.session_file(NODE, "40000", "t", "666")

    with pytest.raises(SystemExit):
        cli.cmd_tunnel_up(Args(name="default", port=8889), cfg())

    assert ssh.forwards == []


def test_bare_down_does_not_ask_when_only_one_session_is_alive(monkeypatch):
    ssh = live_plus_dead()
    monkeypatch.setattr(cli, "is_interactive", lambda: True)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("prompted about one live session"))

    cli.cmd_down(Args(all=False, name=None), cfg())

    assert ssh.scancelled == [JOB]
