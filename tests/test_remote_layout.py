"""The cluster session-file naming convention: session.<name>@<job id>.

The job id in the filename is what makes a leftover unambiguous. Files written
before the convention (session.<name>) carry their job id inside instead and
must keep working, or a session that is live right now breaks on upgrade.
"""
from __future__ import annotations

import fakecluster as fc

cli = fc.load_cli()
fc.bind_errors(cli)

DEAD, LIVE = "100", "200"



def cfg():
    return cli.Config(cli.DEFAULTS | {"host": "fake"})


def test_filename_round_trip():
    assert cli.parse_session_filename("session.default@12345") == ("default", "12345")
    assert cli.parse_session_filename("session.default") == ("default", "")
    assert cli.parse_session_filename("env.sh") is None
    assert cli.remote_session_file("/d", "default", "9").endswith("/session.default@9")
    assert cli.remote_session_file("/d", "default").endswith("/session.default")


def test_dotted_session_names_stay_unambiguous():
    """Names may contain dots (NAME_RE), so a dot-separated job id would be
    ambiguous: 'session.exp.2' is a legacy file for a session called 'exp.2',
    not job 2 of session 'exp'. That is why the separator is '@'."""
    assert cli.parse_session_filename("session.exp.2") == ("exp.2", "")
    assert cli.parse_session_filename("session.exp.2@77") == ("exp.2", "77")


def test_selection_prefers_tracked_then_live_then_newest():
    files = {DEAD: {"JOB_ID": DEAD}, LIVE: {"JOB_ID": LIVE}}
    assert cli.select_session_file(files, {LIVE}, DEAD)[0] == DEAD, "the job we track"
    assert cli.select_session_file(files, {LIVE}, "")[0] == LIVE, "the live one"
    assert cli.select_session_file(files, set(), "")[0] == LIVE, "else the newest"
    assert cli.select_session_file({}, set(), "")[1] == {}


def test_leftover_cannot_shadow_a_live_session():
    """The point of the convention. Both files exist under one name; the dead
    job's file must not be what a fresh adopt reads."""
    ssh = fc.FakeSSH(files={f"session.default@{DEAD}": fc.session_file(node="old", job=DEAD),
                            f"session.default@{LIVE}": fc.session_file(node="new", job=LIVE)},
                     jobs={LIVE: fc.running("new")})

    view = cli.sync_one("default", ssh, cli.Slurm(ssh), cfg(), max_age_s=0)

    assert view.status == "live"
    assert view.payload["JOB_ID"] == LIVE and view.payload["NODE"] == "new"


def test_legacy_file_still_works():
    """A session launched before this change has no job id in its filename."""
    ssh = fc.FakeSSH(files={"session.default": fc.session_file(node="n1", job=LIVE)},
                     jobs={LIVE: fc.running("n1")})

    view = cli.sync_one("default", ssh, cli.Slurm(ssh), cfg(), max_age_s=0)

    assert view.status == "live"
    assert view.payload["JOB_ID"] == LIVE and view.payload["NODE"] == "n1"


def test_legacy_file_is_cleaned_when_its_job_ends():
    ssh = fc.FakeSSH(files={"session.default": fc.session_file(job=DEAD)})

    view = cli.sync_one("default", ssh, cli.Slurm(ssh), cfg(), max_age_s=0)

    assert view.status == "stale"
    assert ssh.files == {}, "the job-less path must be removable too"


def test_sweep_drops_dead_leftovers_but_keeps_the_live_one():
    ssh = fc.FakeSSH(files={f"session.default@{DEAD}": fc.session_file(job=DEAD),
                            f"session.default@{LIVE}": fc.session_file(job=LIVE),
                            "session.other@300": fc.session_file(job="300")},
                     jobs={LIVE: fc.running(), "300": fc.running()})

    cli.sync_all(ssh, cli.Slurm(ssh), cfg())

    assert f"session.default@{DEAD}" not in ssh.files
    assert f"session.default@{LIVE}" in ssh.files
    assert "session.other@300" in ssh.files


def test_down_removes_every_file_for_the_name_and_only_that_name():
    """The per-name pattern is anchored on '@', or `down --name default` would
    also delete the files of a session called 'default2'."""
    ssh = fc.FakeSSH(files={f"session.default@{DEAD}": fc.session_file(job=DEAD),
                            f"session.default@{LIVE}": fc.session_file(job=LIVE),
                            "session.default": fc.session_file(job="1"),
                            "session.default2@300": fc.session_file(job="300"),
                            "session.keep@9": fc.session_file(job="9")})

    cli.remove_all_remote_session_files("default", ssh, cfg())

    assert sorted(ssh.files) == ["session.default2@300", "session.keep@9"]


def test_name_prefix_collision_does_not_leak():
    """A session called 'default2' must be invisible to 'default', both in the
    listing pattern and in what gets selected."""
    ssh = fc.FakeSSH(files={"session.default2@300": fc.session_file(job="300")},
                     jobs={"300": fc.running()})

    view = cli.sync_one("default", ssh, cli.Slurm(ssh), cfg(), max_age_s=0)

    assert view.status == "missing"
    assert "session.default2@300" in ssh.files
