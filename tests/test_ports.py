"""Choosing the local port.

`up` is normally run right after the previous session was cleaned up, which is
the one moment when the port it wants looks busy for reasons that don't matter.
"""
from __future__ import annotations

import socket

import fakecluster as fc

cli = fc.load_cli()


def closed_with_time_wait() -> int:
    """A port whose listener is gone but whose last connection is in TIME_WAIT
    — what `localhost:8888` looks like for a minute after the tunnel serving an
    open browser tab is cancelled."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    client = socket.socket()
    client.connect(("127.0.0.1", port))
    conn, _ = srv.accept()
    conn.close()               # close server-side first: this side goes TIME_WAIT
    client.close()
    srv.close()
    return port


def test_time_wait_is_not_in_use():
    """ssh sets SO_REUSEADDR on its -L listeners, so it can bind straight over
    TIME_WAIT. Probing without it made `up` skip the port it had just freed and
    quietly hand the user a different URL."""
    assert cli.port_in_use(closed_with_time_wait()) is False


def test_pick_free_port_keeps_the_requested_port_after_a_tunnel_closes():
    port = closed_with_time_wait()
    assert cli.pick_free_port(port) == port


def test_a_real_listener_is_still_in_use():
    for host in ("127.0.0.1", "0.0.0.0"):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((host, 0))
        s.listen(1)
        try:
            assert cli.port_in_use(s.getsockname()[1]) is True, host
        finally:
            s.close()


def test_free_port_is_free():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    assert cli.port_in_use(port) is False


def test_pick_free_port_skips_ports_reserved_by_other_sessions():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    base = s.getsockname()[1]
    s.close()
    assert cli.pick_free_port(base, reserved={base}) == base + 1


JOB, NODE = "555", "c01n02"


def cfg():
    return cli.Config(cli.DEFAULTS | {"host": "fake"})


def live_plus_dead():
    """One running session on 8888, plus the local file of an ended job still
    claiming 8889."""
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(NODE, "51577", "t", JOB)},
                     jobs={JOB: fc.running(NODE)})
    fc.install(cli, ssh)
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888",
                                  "NODE": NODE, "PORT": "51577", "TOKEN": "t"})
    cli.Sessions.save("job2", {"JOB_ID": "23710782", "LOCAL_PORT": "8889"})
    return ssh


def test_a_dead_session_stops_reserving_its_port():
    """Local files outlive their jobs, so counting them all reserved 8889 for a
    job that had ended - `up` then moved to another port and changed the URL."""
    ssh = live_plus_dead()

    live = cli.port_holding_names("new", ssh, cli.Slurm(ssh), cfg())

    assert live == ["default"]
    assert cli.reserved_local_ports(exclude_name="new", names=live) == {8888}


def test_a_live_session_still_reserves_its_port():
    ssh = live_plus_dead()
    ssh.jobs["666"] = fc.running("c02n01")
    ssh.files["session.job2@666"] = fc.session_file("c02n01", "40000", "t2", "666")
    cli.Sessions.save("job2", {"JOB_ID": "666", "LOCAL_PORT": "8889"})

    live = cli.port_holding_names("new", ssh, cli.Slurm(ssh), cfg())

    assert cli.reserved_local_ports(exclude_name="new", names=live) == {8888, 8889}


def test_a_dead_session_is_not_blamed_for_holding_a_port():
    ssh = live_plus_dead()

    live = cli.port_holding_names("new", ssh, cli.Slurm(ssh), cfg())

    assert cli.tracked_forward_owner(8889, names=live) == ("", "")


def test_a_lone_session_costs_no_round_trip():
    """The reservation set is empty with nothing else on disk, so `up` must not
    pay for a cluster query to find that out."""
    ssh = fc.FakeSSH(files={f"session.default@{JOB}": fc.session_file(NODE, "51577", "t", JOB)},
                     jobs={JOB: fc.running(NODE)})
    fc.install(cli, ssh)
    cli.Sessions.save("default", {"JOB_ID": JOB, "LOCAL_PORT": "8888"})

    assert cli.port_holding_names("default", ssh, cli.Slurm(ssh), cfg()) == ["default"]
    assert ssh.commands == []
