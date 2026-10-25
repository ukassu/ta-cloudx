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

from scenario_base import LogWriter, Scenario, add_common_args, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--max-think-sec", type=float, default=60,
                    help="jeda berpikir maksimum antar cell (di bawah ambang idle)")
    ap.add_argument("--max-burst-sec", type=float, default=15)
    ap.add_argument("--width", type=int, default=2048)
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = torch.nn.Sequential(
        torch.nn.Linear(1024, args.width), torch.nn.ReLU(), torch.nn.Linear(args.width, 10),
    ).to(device)

    start = time.time()
    while time.time() - start < args.duration_sec:
        touch_activity()
        cell = random.choice(["gpu", "gpu", "cpu"])
        burst_end = time.time() + random.uniform(2, args.max_burst_sec)
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
        log.tick()
        time.sleep(random.uniform(5, args.max_think_sec))

    print("[benign_interactive_gpu_notebook] selesai.")


register(Scenario(
    name="benign_interactive_gpu_notebook",
    label="benign",
    script_path="scenarios/benign_interactive_gpu_notebook.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=None,
    description="Burst GPU/CPU singkat per cell diselingi jeda berpikir; "
                "aktivitas notebook rutin.",
    params={
        "duration_sec": (180, 600),
        "max_think_sec": (20, 100),  # tetap di bawah ambang idle 120 s
        "max_burst_sec": (5, 20),
        "width": [1024, 2048, 4096],
    },
    queue_waiters=(0, 1),
    sibling_sessions=(0, 1),
))


if __name__ == "__main__":
    main()
