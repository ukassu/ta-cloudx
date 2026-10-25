"""
scenarios/benign_batch_inference.py
======================================
Job BENIGN: inferensi batch -- model dimuat sekali, lalu memproses
batch data yang datang tidak teratur (ukuran dan jeda acak) dan
(opsional) menulis hasil prediksi ke disk. GPU bursty.
"""
import argparse
import random
import time

import numpy as np
import torch
import torch.nn as nn

from scenario_base import LogWriter, Scenario, add_common_args, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--max-batch", type=int, default=256)
    ap.add_argument("--max-gap-sec", type=float, default=10)
    ap.add_argument("--image-size", type=int, default=64)
    ap.add_argument("--save-preds", type=int, default=1, help="1 = tulis prediksi ke disk")
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = nn.Sequential(
        nn.Conv2d(3, 64, 3, padding=1), nn.ReLU(),
        nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(), nn.AdaptiveAvgPool2d(4),
        nn.Flatten(), nn.Linear(64 * 16, 100),
    ).to(device).eval()

    size = args.image_size
    start = time.time()
    n_batch = 0
    while time.time() - start < args.duration_sec:
        touch_activity()
        batch = random.randint(32, args.max_batch)
        with torch.no_grad():
            for _ in range(random.randint(30, 120)):
                preds = model(torch.randn(batch, 3, size, size, device=device)).argmax(1)
        if args.save_preds:
            np.save(f"/tmp/preds_{n_batch % 10}.npy", preds.cpu().numpy())
        n_batch += 1
        log.tick()
        time.sleep(random.uniform(2, args.max_gap_sec))   # menunggu batch berikutnya

    print(f"[benign_batch_inference] selesai, {n_batch} batch.")


register(Scenario(
    name="benign_batch_inference",
    label="benign",
    script_path="scenarios/benign_batch_inference.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=None,
    description="Burst GPU per batch dengan jeda tidak teratur.",
    params={
        "duration_sec": (180, 600),
        "max_batch": (64, 256),   # di GPU 4 GB tetap di bawah kuota sesi
        "max_gap_sec": (5, 20),
        "image_size": [32, 48, 64],
        "save_preds": [0, 1],
    },
    queue_waiters=(0, 1),
))


if __name__ == "__main__":
    main()
