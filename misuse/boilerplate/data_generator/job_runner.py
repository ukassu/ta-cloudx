"""
data_generator/job_runner.py
===============================
Menjalankan SATU sesi job dari sebuah Scenario (lihat scenario_base.py)
dan merekam telemetrinya. GENERIK terhadap skenario apapun -- menerima
objek Scenario, bukan path/label/timeout terpisah, supaya seluruh
metadata skenario (termasuk timeout_sec bawaan skenario) konsisten
dipakai tanpa duplikasi di run_experiment.py.
"""

import os
import subprocess
import sys
import time

from scenario_base import Scenario
from telemetry_logger import TelemetryLogger


def run_labeled_job(scenario: Scenario, session_id: str, out_dir="data",
                     interval_sec=5.0, args_override=None, extra_meta=None,
                     gpu_index=0, **_k8s_only):
    """
    Parameters
    ----------
    scenario : Scenario   Objek skenario dari registry (get() atau
                          all_scenarios()).
    session_id : str      ID unik sesi (pastikan unik antar pemanggilan
                          run_experiment.py, mis. sertakan running index).
    args_override : list[str], optional  Ganti scenario.default_args
                          jika ingin variasi parameter per pemanggilan
                          (mis. durasi acak).
    extra_meta : dict, optional  Metadata tambahan untuk baris CSV
                          (mis. claimed_quota_mb untuk skenario yang
                          membutuhkannya -- lihat
                          scenarios/misuse_quota_exceed.py).

    **_k8s_only : n_waiters, n_siblings, job_requests -- konteks cluster
                          yang hanya bermakna di k8s_job_runner; diabaikan.

    Returns
    -------
    str  path file CSV hasil rekaman sesi ini.
    """
    args = args_override if args_override is not None else scenario.default_args
    out_path = f"{out_dir}/{session_id}.csv"

    env = os.environ.copy()
    module_dir = os.path.dirname(os.path.abspath(__file__))
    env["PYTHONPATH"] = os.pathsep.join(
        part for part in (module_dir, env.get("PYTHONPATH", "")) if part
    )
    proc = subprocess.Popen(
        [sys.executable, scenario.script_path] + args,
        env=env,
    )
    logger = TelemetryLogger(
        root_pid=proc.pid, session_id=session_id, label=scenario.label,
        job_type=scenario.name, out_path=out_path, interval_sec=interval_sec,
        gpu_index=gpu_index, extra_meta=extra_meta,
    )
    logger.start()

    start_time = time.time()
    timeout_sec = scenario.timeout_sec
    try:
        while proc.poll() is None:
            if timeout_sec is not None and (time.time() - start_time) > timeout_sec:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                break
            time.sleep(1)
    finally:
        logger.stop()

    print(f"[job_runner] session={session_id} scenario={scenario.name} "
          f"label={scenario.label} -> {out_path}")
    return out_path
