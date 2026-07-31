"""Diagnosing a refused port forward.

`ssh -O forward` hands the request to the ControlMaster, which keeps the real
cause ("Address already in use") on its own stderr; the client only ever sees
"Port forwarding failed". Pattern-matching that text never worked, so the cause
is established locally instead.
"""
from __future__ import annotations

import socket

import fakecluster as fc

cli = fc.load_cli()
fc.bind_errors(cli)

MUX = ("tunnel forward failed: mux_client_forward: forwarding request failed: "
       "Port forwarding failed\nmuxclient: master forward request failed")
HINT = "pass a different port"



def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Bound:
    """Hold a real local port for the duration of a test."""

    def __enter__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        return self.sock.getsockname()[1]

    def __exit__(self, *exc):
        self.sock.close()


def test_names_the_process_holding_the_port():
    with Bound() as port:
        msg = cli.forward_failure_reason("bouchet", port, MUX, HINT)
    assert f"local port {port} is already in use" in msg
    assert HINT in msg
    assert "mux_client_forward" not in msg, "the mux text explains nothing to a user"


def test_names_our_own_forward_and_how_to_free_it():
    with Bound() as port:
        cli.Sessions.save("work", {"JOB_ID": "1", "LOCAL_PORT": "9999",
                                   "FORWARDS": f"{port}:9000"})
        msg = cli.forward_failure_reason("bouchet", port, MUX, HINT)
    assert "the forward from session 'work' to node port 9000" in msg
    assert f"bouchet-jupyter forward down {port} --name work" in msg


def test_names_another_sessions_tunnel():
    with Bound() as port:
        cli.Sessions.save("work", {"JOB_ID": "1", "LOCAL_PORT": str(port)})
        msg = cli.forward_failure_reason("bouchet", port, MUX, HINT)
    assert "the jupyter tunnel for session 'work'" in msg


def test_a_session_does_not_collide_with_itself():
    """`tunnel up` re-opens its own port; blaming the session for holding it
    would send the user chasing their own tunnel."""
    with Bound() as port:
        cli.Sessions.save("work", {"JOB_ID": "1", "LOCAL_PORT": str(port)})
        msg = cli.forward_failure_reason("bouchet", port, MUX, HINT,
                                         exclude_tunnel_of="work")
    assert "jupyter tunnel for session 'work'" not in msg


def test_privileged_port_is_reported_as_such():
    """port_in_use() cannot tell EACCES from EADDRINUSE, so this must be
    checked first or it reads as "in use by another process"."""
    msg = cli.forward_failure_reason("bouchet", 80, MUX, HINT)
    assert "privileged" in msg


def test_free_port_keeps_the_raw_error():
    """Nothing local explains it, so don't invent a cause: point at the master
    and hand back what ssh said."""
    msg = cli.forward_failure_reason("bouchet", free_port(), MUX, HINT)
    assert "ssh -O check bouchet" in msg
    assert "mux_client_forward" in msg


def test_up_and_adopt_do_not_bypass_the_diagnosis():
    """Both called ssh.forward() raw, so the useless mux text reached the user
    at the worst moment: job allocated, tunnel the last remaining step."""
    src = cli.__file__ and open(cli.__file__).read()
    body = src.split("def cmd_up(")[1].split("\ndef ")[0]
    assert "forward_or_die(" in body and "ssh.forward(" not in body
    adopt = src.split("def cmd_adopt(")[1].split("\ndef ")[0]
    assert "forward_failure_reason(" in adopt
