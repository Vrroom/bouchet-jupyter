"""Reverse forwards: a dedicated ssh into the node, tracked by pid.

The cluster side is the usual FakeSSH. The node-facing pieces that need a real
network (user lookup, the ss probe on the node, the detached ssh) are replaced
per test by NodeSeams; the pid check is exercised against real processes.
"""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

import fakecluster as fc

cli = fc.load_cli()
fc.bind_errors(cli)

JOB, NODE = "555", "c01n02"
LISTENING = "LISTEN 0 128 127.0.0.1:8765 0.0.0.0:*"



def cfg():
    return cli.Config(cli.DEFAULTS | {"host": "fake"})


class Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeProc:
    """Stands in for the Popen of the detached ssh. `returncode` is what
    poll() reports: None while it runs, an exit code once it has died."""

    def __init__(self, pid=4242, returncode=None):
        self.pid = pid
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True


class NodeSeams:
    """What the node-facing functions answer and what they were asked."""

    def __init__(self):
        self.before_start = ""          # ss line on the node before our ssh starts
        self.after_start = LISTENING    # ss line once our ssh is up
        self.proc = FakeProc()
        self.log_text = ""              # what the fake ssh leaves in its log
        self.started: list[list[str]] = []
        self.stopped: list[tuple] = []
        self.stop_result = True

    def listener(self, host, user, node, port):
        return self.after_start if self.started else self.before_start

    def start(self, argv, log):
        self.started.append(argv)
        if self.log_text:
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(self.log_text)
        return self.proc

    def stop(self, pid, node, lport, rport):
        self.stopped.append((pid, node, lport, rport))
        return self.stop_result


@pytest.fixture
def node(monkeypatch):
    seams = NodeSeams()
    monkeypatch.setattr(cli, "ssh_user_for", lambda host: "sc3344")
    monkeypatch.setattr(cli, "node_listener", seams.listener)
    monkeypatch.setattr(cli, "start_reverse_forward", seams.start)
    monkeypatch.setattr(cli, "stop_reverse_forward", seams.stop)
    return seams


def live_session(**local):
    """A FakeSSH with one running job, installed as the CLI's SSH, plus any
    extra local state keys (FORWARDS, REVERSE_FORWARDS) for 'default'."""
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(NODE, "46337", "tok", JOB)},
                     jobs={JOB: fc.running(NODE)})
    fc.install(cli, ssh)
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888", "NODE": NODE,
                                  "PORT": "46337", "TOKEN": "tok", **local})
    return ssh


def up(spec="8765"):
    cli.cmd_forward_up(Args(ports=[spec], remote=None, node=None, name=None, reverse=True), cfg())


def down(*ports, reverse=False):
    cli.cmd_forward_down(Args(ports=list(ports), name=None, reverse=reverse), cfg())


def state():
    return cli.Sessions.load("default")


def test_reverse_up_records_state_and_prints_the_node_side_address(node, capsys):
    live_session()

    up("8765")

    assert cli.parse_reverse_forwards(state()["REVERSE_FORWARDS"]) == [(8765, 8765, 4242)]
    assert "FORWARDS" not in state(), "a reverse forward is not a -L forward"
    argv = node.started[0]
    assert argv[-3:] == ["-J", "fake", f"sc3344@{NODE}"], "user is explicit, host is the jump"
    assert argv[argv.index("-R") + 1] == "8765:localhost:8765"
    assert "-N" in argv and "ExitOnForwardFailure=yes" in argv and "BatchMode=yes" in argv
    assert "dial from the node: localhost:8765" in capsys.readouterr().out


def test_reverse_up_with_a_distinct_node_port(node, capsys):
    live_session()

    up("8765:9000")

    assert cli.parse_reverse_forwards(state()["REVERSE_FORWARDS"]) == [(8765, 9000, 4242)]
    assert "9000:localhost:8765" in node.started[0]
    assert "dial from the node: localhost:9000" in capsys.readouterr().out


def test_reverse_up_takes_exactly_one_spec(node):
    live_session()
    with pytest.raises(SystemExit):
        cli.cmd_forward_up(Args(ports=["8765", "8766"], remote=None, node=None, name=None,
                                reverse=True), cfg())
    with pytest.raises(SystemExit):
        cli.cmd_forward_up(Args(ports=["8765"], remote=9000, node=None, name=None,
                                reverse=True), cfg())
    assert node.started == []


def test_busy_node_port_is_named_before_anything_starts(node, capsys):
    live_session()
    node.before_start = LISTENING

    with pytest.raises(SystemExit):
        up("8765")

    assert node.started == [], "nothing to kill later: the ssh was never started"
    err = capsys.readouterr().err
    assert f"node port 8765 on {NODE} is already in use" in err
    assert "--reverse 8765:8766" in err
    assert "REVERSE_FORWARDS" not in state()


def test_ssh_that_exits_is_reported_with_its_own_output(node, capsys):
    live_session()
    node.proc = FakeProc(returncode=255)
    node.after_start = ""
    node.log_text = f"sc3344@{NODE}: Permission denied (publickey)."

    with pytest.raises(SystemExit):
        up("8765")

    err = capsys.readouterr().err
    assert "ssh exited 255" in err and "Permission denied" in err
    assert "REVERSE_FORWARDS" not in state()


def test_list_shows_both_directions(node, capsys):
    live_session(FORWARDS="8082:8082", REVERSE_FORWARDS="8765:9000:4242")

    cli.cmd_forward_list(Args(name=None), cfg())

    out = capsys.readouterr().out
    assert f"localhost:8082 -> {NODE}:8082" in out
    assert f"{NODE} localhost:9000 -> localhost:8765  (reverse, pid 4242 not running)" in out


def test_forward_down_closes_a_reverse_forward_by_its_node_port(node, capsys):
    ssh = live_session(FORWARDS="8082:8082", REVERSE_FORWARDS="8765:9000:4242")

    down(9000)

    assert node.stopped == [(4242, NODE, 8765, 9000)]
    assert ssh.cancelled == [], "the -L forward on 8082 is untouched"
    assert "REVERSE_FORWARDS" not in state()
    assert state()["FORWARDS"] == "8082:8082"
    assert "Reverse forward down" in capsys.readouterr().out


def test_forward_down_refuses_a_port_that_names_both_directions(node):
    ssh = live_session(FORWARDS="8765:8765", REVERSE_FORWARDS="8765:8765:4242")

    with pytest.raises(SystemExit):
        down(8765)
    assert node.stopped == [] and ssh.cancelled == []

    down(8765, reverse=True)

    assert node.stopped == [(4242, NODE, 8765, 8765)]
    assert state()["FORWARDS"] == "8765:8765"
    assert "REVERSE_FORWARDS" not in state()


def test_down_stops_the_reverse_forward(node):
    ssh = live_session(REVERSE_FORWARDS="8765:8765:4242")

    cli.cmd_down(Args(all=False, name="default"), cfg())

    assert node.stopped == [(4242, NODE, 8765, 8765)]
    assert ssh.scancelled == [JOB]


def test_stale_cleanup_stops_the_reverse_forward(node):
    """The job ended behind our back; the ssh into its node is dead or dying
    and must not stay tracked."""
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888",
                                  "REVERSE_FORWARDS": "8765:8765:4242"})
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(NODE, "46337", "tok", JOB)})
    fc.install(cli, ssh)

    view = cli.sync_all(ssh, cli.Slurm(ssh), cfg())["default"]

    assert view.status == "stale"
    assert node.stopped == [(4242, NODE, 8765, 8765)]


def test_a_recycled_pid_is_not_killed():
    """The pid in session state may now belong to anything."""
    p = subprocess.Popen(["sleep", "30"])
    try:
        assert cli.stop_reverse_forward(p.pid, NODE, 8765, 8765) is False
        assert p.poll() is None, "the unrelated process is untouched"
    finally:
        p.kill()
        p.wait()


def test_our_own_ssh_is_recognised_and_killed(tmp_path):
    """A real process whose argv looks like ours: python behind a symlink
    named ssh, carrying the -R spec and the user@node destination."""
    link = tmp_path / "ssh"
    link.symlink_to(sys.executable)
    p = subprocess.Popen([str(link), "-c", "import time; time.sleep(30)",
                          "8765:localhost:8765", "-J", "fake", f"sc3344@{NODE}"])
    try:
        # Popen returns at the exec call, a moment before the kernel publishes
        # the new argv in /proc, so an immediate read can come back empty.
        for _ in range(100):
            if cli.reverse_forward_cmdline(p.pid):
                break
            time.sleep(0.01)
        assert cli.is_reverse_forward_process(cli.reverse_forward_cmdline(p.pid), NODE, 8765, 8765)
        assert not cli.is_reverse_forward_process(cli.reverse_forward_cmdline(p.pid), "other", 8765, 8765)
        assert not cli.is_reverse_forward_process(cli.reverse_forward_cmdline(p.pid), NODE, 8765, 9000)
        assert cli.stop_reverse_forward(p.pid, NODE, 8765, 8765) is True
        assert p.wait(timeout=5) == -15
    finally:
        if p.poll() is None:
            p.kill()
            p.wait()
