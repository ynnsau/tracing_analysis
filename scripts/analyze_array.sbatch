#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 || -z "${SLURM_ARRAY_TASK_ID:-}" || -z "${SLURM_CPUS_PER_TASK:-}" ]]; then
  echo 'Submit using: python3 scripts/workflow.py submit --run-dir PREPARED_RUN' >&2
  exit 2
fi
run_dir=$1
export OMP_NUM_THREADS=1
export OPENBLAS_NUM_THREADS=1
export PYTHONUNBUFFERED=1

record_failure() {
  local code=$?
  if (( code != 0 )); then
    python3 "${run_dir}/snapshot/scripts/workflow.py" step-failed \
      --run-dir "${run_dir}" --index "${SLURM_ARRAY_TASK_ID}" --exit-code "${code}" || true
  fi
}
trap record_failure EXIT

# One independent srun step per array element. Placement is left to Slurm.
srun --exclusive --exact --nodes=1 --ntasks=1 \
  --cpus-per-task="${SLURM_CPUS_PER_TASK}" --kill-on-bad-exit=1 \
  python3 "${run_dir}/snapshot/scripts/workflow.py" task \
  --run-dir "${run_dir}" --index "${SLURM_ARRAY_TASK_ID}"
