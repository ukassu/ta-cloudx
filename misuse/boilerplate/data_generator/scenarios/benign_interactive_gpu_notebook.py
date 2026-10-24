"""
scenarios/benign_interactive_gpu_notebook.py
===============================================
Job BENIGN: notebook GPU interaktif. Pengguna menjalankan cell secara
bergantian (komputasi GPU singkat, olah data CPU) diselingi jeda
berpikir. Rata-rata utilisasi GPU rendah TETAPI aktivitas notebook
rutin -- pembanding sulit (hard negative) untuk R1/R2: GPU "menganggur"
di sela cell bukan berarti hoarding.
"""
import argparse
import random
import time

import numpy as np
import torch

from scenario_base import Scenario, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--max-think-sec", type=int, default=60,
                    help="jeda berpikir maksimum antar cell (di bawah ambang idle)")
    args = ap.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = torch.nn.Sequential(
        torch.nn.Linear(1024, 2048), torch.nn.ReLU(), torch.nn.Linear(2048, 10),
    ).to(device)

    start = time.time()
    while time.time() - start < args.duration_sec:
        touch_activity()
        cell = random.choice(["gpu", "gpu", "cpu"])
        burst_end = time.time() + random.uniform(3, 15)
        while time.time() < burst_end:
            if cell == "gpu":
                x = torch.randn(512, 1024, device=device)
                model(x).sum().backward()
            else:
                data = np.random.randn(100_000, 8)
                _ = np.percentile(data, [5, 50, 95], axis=0)
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        touch_activity()
        time.sleep(random.uniform(5, args.max_think_sec))

    print("[benign_interactive_gpu_notebook] selesai.")


register(Scenario(
    name="benign_interactive_gpu_notebook",
    label="benign",
    script_path="scenarios/benign_interactive_gpu_notebook.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=None,
    description="Burst GPU/CPU singkat per cell diselingi jeda 5-60 s; "
                "aktivitas notebook rutin.",
    queue_waiters=(0, 1),
))


if __name__ == "__main__":
    main()
