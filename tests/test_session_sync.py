"""Reconciling local state against the cluster.

Every case here is a bug that shipped: the local record and the cluster file
disagreed, and a command decided from the wrong one or from a read it had
already invalidated.
"""
from __future__ import annotations

import fakecluster as fc

cli = fc.load_cli()
fc.bind_errors(cli)

DEAD, LIVE = "20616947", "20617111"
NODE = "a1126u11n01.mghpcc.ycrc.yale.edu"



def cfg():
    return cli.Config(cli.DEFAULTS | {"host": "fake"})


def track(job=DEAD, **extra):
    cli.Sessions.save("default", {"JOB_ID": job, "PROFILE": "gpu",
                                  "LOCAL_PORT": "8888", "SUBMITTED_AT": "1", **extra})


def sync_all(ssh):
    return cli.sync_all(ssh, cli.Slurm(ssh), cfg())


def sync_one(ssh, name="default"):
    return cli.sync_one(name, ssh, cli.Slurm(ssh), cfg(), max_age_s=0)


def test_ended_job_clears_local_and_cluster_state():
    """`status` used to delete only the local record. The cluster file it left
    behind then made the next `up` dead-end on stale_unverified, because the
    JOB_ID needed to check squeue/sacct had just been thrown away."""
    track()
    ssh = fc.FakeSSH(files={f"session.default@{DEAD}": fc.session_file(job=DEAD)})
    view = sync_all(ssh)["default"]

    assert view.status == "stale"
    assert view.notes == [f"[default] cleaned stale (job {DEAD} ended)"]
    assert ssh.files == {}, "cluster file must go too, or it resurrects the session"
    assert sync_one(ssh).status == "missing", "so `up` has a clean slate"


def test_cluster_only_leftover_is_verified_not_guessed():
    """With no local record, the job id has to come from the cluster file.
    Without that fallback every command reported stale_unverified forever."""
    ssh = fc.FakeSSH(files={f"session.default@{DEAD}": fc.session_file(job=DEAD)})

    view = sync_one(ssh)

    assert view.status == "stale"
    assert ssh.files == {}


def test_dead_record_does_not_mask_a_newer_live_job():
    """`adopt` printed "(nothing to adopt)" and then adopted on the very next
    run: the dead record was classified, cleaned, and the live cluster session
    it was hiding never got looked at."""
    track(job=DEAD)
    ssh = fc.FakeSSH(files={f"session.default@{LIVE}": fc.session_file(NODE, "46337", "tok", LIVE)},
                     jobs={LIVE: fc.running(NODE)})

    view = sync_all(ssh)["default"]

    assert view.status == "live"
    assert view.payload["JOB_ID"] == LIVE
    assert view.payload["NODE"] == NODE and view.payload["PORT"] == "46337"
    assert view.notes == [f"[default] cleaned stale (job {DEAD} ended)"]
    assert ssh.files, "the live session's file must survive"


def test_recheck_keeps_laptop_side_preferences():
    """Adopting the newer job must not silently move the user's local port."""
    track(job=DEAD, LOCAL_PORT="8891")
    ssh = fc.FakeSSH(files={f"session.default@{LIVE}": fc.session_file(job=LIVE)},
                     jobs={LIVE: fc.running()})

    view = sync_all(ssh)["default"]

    assert view.payload["LOCAL_PORT"] == "8891"
    assert view.payload["PROFILE"] == "gpu"
    assert "SUBMITTED_AT" not in view.payload, "would fake a submit grace window"


def test_sync_one_and_sync_all_agree():
    """They classified the same session differently, so `status` and `up`
    disagreed about whether a session existed."""
    track(job=DEAD)
    files = {f"session.default@{LIVE}": fc.session_file(job=LIVE)}
    one = sync_one(fc.FakeSSH(files=dict(files), jobs={LIVE: fc.running()}))
    for p in cli.STATE_DIR.glob("*"):        # same starting state for both
        p.unlink()
    track(job=DEAD)
    all_ = sync_all(fc.FakeSSH(files=dict(files), jobs={LIVE: fc.running()}))["default"]

    assert one.status == all_.status == "live"
    assert one.payload["JOB_ID"] == all_.payload["JOB_ID"] == LIVE


def test_both_jobs_dead_cleans_both_and_terminates():
    track(job=DEAD)
    ssh = fc.FakeSSH(files={f"session.default@{LIVE}": fc.session_file(job=LIVE)})

    view = sync_all(ssh)["default"]

    assert view.status == "stale"
    assert len(view.notes) == 2, "one note per dead job"
    assert ssh.files == {}


def test_live_session_of_another_job_is_never_deleted():
    """Cleaning up after our dead job must not touch a session someone started
    from another machine under the same name."""
    track(job=DEAD)
    other = f"session.default@{LIVE}"
    ssh = fc.FakeSSH(files={other: fc.session_file(job=LIVE)}, jobs={LIVE: fc.running()})

    sync_all(ssh)

    assert other in ssh.files


def test_unreachable_cluster_keeps_cached_state():
    track(job=LIVE, TOKEN="t", NODE="n", PORT="1")
    ssh = fc.FakeSSH()
    ssh.fail = True

    view = sync_one(ssh)

    assert view.status == "unknown"
    assert view.payload["JOB_ID"] == LIVE, "must not discard state we cannot verify"
    assert cli.Sessions.load("default"), "and must not delete the local record"
