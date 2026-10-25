"""
scenarios/benign_eda_notebook.py
===================================
Job BENIGN: eksplorasi data ala notebook (pandas/numpy), CPU-bound dan
GPU rendah/nol -- contoh "job wajar yang memang tidak butuh GPU tinggi".
"""
import argparse
import time

import numpy as np
import pandas as pd

from scenario_base import LogWriter, Scenario, add_common_args, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=300)
    ap.add_argument("--rows", type=int, default=200_000)
    ap.add_argument("--pause-sec", type=float, default=1.0)
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    start = time.time()
    while time.time() - start < args.duration_sec:
        df = pd.DataFrame(
            np.random.randn(args.rows, 8),
            columns=[f"col{i}" for i in range(8)],
        )
        df["group"] = np.random.randint(0, 20, size=len(df))
        _ = df.groupby("group").agg(["mean", "std", "max"])
        _ = df.corr()
        touch_activity()
        log.tick()
        time.sleep(args.pause_sec)

    print("[benign_eda_notebook] selesai.")


register(Scenario(
    name="benign_eda_notebook",
    label="benign",
    script_path="scenarios/benign_eda_notebook.py",
    default_args=["--duration-sec", "300"],
    timeout_sec=None,
    description="CPU tinggi, GPU nyaris 0% -- penyeimbang kelas benign non-GPU.",
    params={
        "duration_sec": (180, 600),
        "rows": (50_000, 400_000),
        "pause_sec": (0.5, 3.0),
    },
    sibling_sessions=(0, 1),  # maks 2 sesi paralel: masih di dalam batas kebijakan
))


if __name__ == "__main__":
    main()
