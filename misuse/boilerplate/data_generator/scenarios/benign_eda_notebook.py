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

from scenario_base import Scenario, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=300)
    ap.add_argument("--rows", type=int, default=200_000)
    args = ap.parse_args()

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
        time.sleep(1)

    print("[benign_eda_notebook] selesai.")


register(Scenario(
    name="benign_eda_notebook",
    label="benign",
    script_path="scenarios/benign_eda_notebook.py",
    default_args=["--duration-sec", "300"],
    timeout_sec=None,
    description="CPU tinggi, GPU nyaris 0% -- penyeimbang kelas benign non-GPU.",
    sibling_sessions=1,  # 2 sesi paralel: masih di dalam batas kebijakan (R6 = 4)
))


if __name__ == "__main__":
    main()
