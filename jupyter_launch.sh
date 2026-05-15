#!/bin/bash
#SBATCH --job-name=jupyter
#SBATCH --qos=normal
#SBATCH --account=pi_jd374
#SBATCH --ntasks=1
#SBATCH --output=job_logs/%x-%j.out
#SBATCH --error=job_logs/%x-%j.out
#
# Resource directives (--partition, --gpus, --cpus-per-task, --mem, --time)
# come from the laptop launcher's profile, passed as `sbatch` CLI flags.
# That keeps this script profile-agnostic (gpu vs cpu vs custom).
#
# How `jupyter` is put on PATH is controlled by ~/.jupyter-cluster/env.sh
# (written by the laptop side). If that file is missing, we fall back to
# loading the JupyterLab module.
#
# Session file is per-session: name comes from $JC_SESSION (default "default")
# exported by the launcher via `sbatch --export=ALL,JC_SESSION=$NAME`.

set -euo pipefail
mkdir -p job_logs

ENV_FILE="$HOME/.jupyter-cluster/env.sh"
if [[ -f "$ENV_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$ENV_FILE"
else
    module load JupyterLab/4.2.5-GCCcore-13.3.0
fi

PORT=$(python3 -c 'import socket; s=socket.socket(); s.bind(("",0)); print(s.getsockname()[1]); s.close()')
TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(24))')
NODE=$(hostname -f)

SESSION_DIR="$HOME/.jupyter-cluster"
SESSION_NAME="${JC_SESSION:-default}"
SESSION_FILE="$SESSION_DIR/session.$SESSION_NAME"
mkdir -p "$SESSION_DIR"

TMP=$(mktemp "$SESSION_DIR/session.$SESSION_NAME.XXXXXX")
cat > "$TMP" <<EOF
NODE=$NODE
PORT=$PORT
TOKEN=$TOKEN
JOB_ID=$SLURM_JOB_ID
EOF
mv "$TMP" "$SESSION_FILE"

# Cleanup must run on normal exit AND on the signals Slurm uses to end a job
# (TERM on scancel / time limit; HUP on node drain). SIGKILL still bypasses
# this — that case is handled by the laptop clearing this file before submit.
# Also: do NOT `exec jupyter` below — exec replaces this shell, removing the
# trap entirely, which is why prior runs orphaned the session file on every
# normal completion.
trap 'rm -f "$SESSION_FILE"' EXIT INT TERM HUP QUIT

echo "------------------------------------------------"
echo "Jupyter job ${SLURM_JOB_ID} on $(hostname)"
echo "Port: $PORT"
echo "Start: $(date)"
echo "------------------------------------------------"

cd "$HOME"
jupyter lab \
    --no-browser \
    --ip=0.0.0.0 \
    --port="$PORT" \
    --ServerApp.token="$TOKEN" \
    --ServerApp.password='' \
    --ServerApp.allow_origin='*'
