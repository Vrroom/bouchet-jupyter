# bouchet-jupyter

Run JupyterLab on Yale's Bouchet cluster from a laptop. Submits a Slurm
job, waits for it, opens an SSH tunnel, prints the browser URL. Also
forwards extra ports — handy when you want to talk to a viser viewer or
similar that's running inside the same job.

## Layout

- `bouchet-jupyter` — the laptop-side CLI (Python, no dependencies on 3.11+)
- `jupyter_launch.sh` — the sbatch script that runs on the cluster
- `config.example.toml` — copy to `~/.config/bouchet-jupyter/config.toml`

## Install

```
ln -s "$PWD/bouchet-jupyter" ~/bin/bouchet-jupyter
mkdir -p ~/.config/bouchet-jupyter
cp config.example.toml ~/.config/bouchet-jupyter/config.toml
```

Make sure `~/bin` is on `PATH`. Python 3.11+ uses stdlib `tomllib`; on
3.10 or older, `pip install --user tomli`.

Edit the config — at minimum `sbatch_dir`, `log_dir`, and `env_cmd`.

SSH config needs ControlMaster so `ssh -O forward` can manage tunnels
through a shared connection:

```
Host bouchet
    HostName bouchet.ycrc.yale.edu
    User <netid>
    ControlMaster auto
    ControlPath ~/.ssh/cm-%r@%h:%p
    ControlPersist 8h
```

## Use

```
bouchet-jupyter up                       # submit, wait, tunnel, open browser
bouchet-jupyter up --profile cpu
bouchet-jupyter up --name foo            # second concurrent session

bouchet-jupyter list                     # tracked sessions, live state
bouchet-jupyter status --name foo
bouchet-jupyter url                      # print http://localhost:.../lab?token=...
bouchet-jupyter logs                     # tail the slurm log

bouchet-jupyter down                     # scancel + close tunnels
bouchet-jupyter down --all
```

After a laptop reboot or network drop:

```
bouchet-jupyter adopt                    # rediscover sessions on the cluster
bouchet-jupyter tunnel up                # reopen the jupyter tunnel
```

Extra port forwards on the same compute node (e.g. for a viewer running
inside the job):

```
bouchet-jupyter forward up 8082          # localhost:8082 -> NODE:8082
bouchet-jupyter forward up 8082 --remote 9000
bouchet-jupyter forward list
bouchet-jupyter forward down 8082
```

These are tracked in session state and torn down by `down`.

Cluster snapshots:

```
bouchet-jupyter inspect gpus             # GPU usage by type
bouchet-jupyter inspect nodes --partition gpu_h200
bouchet-jupyter inspect wait             # start-time estimate for pending jobs
```

## Freshness

Every session-touching command (`up`, `down`, `list`, `status`, `url`,
`logs`, `tunnel up`, `forward up/down`, `adopt`) re-pulls the cluster's
view of the session before acting, with a 5-second freshness window —
repeated commands within that window reuse the previous sync. `up` and
`tunnel up` always force-refresh.

When the cluster confirms a session's job has ended (squeue absent +
sacct terminal), the local session file and any open tunnel are cleaned
up automatically and a `[<name>] cleaned stale (job <ID> ended)` note is
printed to stderr. If sacct is silent (lagging or disabled), the local
state is kept as `stale_unverified` — never deleted without proof of
death.

Pass `--no-sync` to `list` or `status` to read cached state without
touching the cluster. Not available on `url` — by design.

## Files

- `~/.config/bouchet-jupyter/config.toml` — paths, profiles, env_cmd
- `~/.local/state/bouchet-jupyter/` — local session and tunnel state
- `~/.jupyter-cluster/` on bouchet — remote session payloads, `env.sh`
