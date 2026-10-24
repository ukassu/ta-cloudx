"""
scenarios/benign_batch_inference.py
======================================
Job BENIGN: inferensi batch -- model dimuat sekali, lalu memproses
batch data yang datang tidak teratur (ukuran dan jeda acak) dan
menulis hasil prediksi ke disk. GPU bursty dengan I/O tulis per batch.
"""
import argparse
import random
import time

import numpy as np
import torch
import torch.nn as nn

from scenario_base import Scenario, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    args = ap.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = nn.Sequential(
        nn.Conv2d(3, 64, 3, padding=1), nn.ReLU(),
        nn.Conv2d(64, 64, 3, padding=1), nn.ReLU(), nn.AdaptiveAvgPool2d(4),
        nn.Flatten(), nn.Linear(64 * 16, 100),
    ).to(device).eval()

    start = time.time()
    n_batch = 0
    while time.time() - start < args.duration_sec:
        touch_activity()
        batch = random.randint(64, 512)
        with torch.no_grad():
            for _ in range(random.randint(5, 20)):
                preds = model(torch.randn(batch, 3, 64, 64, device=device)).argmax(1)
        np.save(f"/tmp/preds_{n_batch % 10}.npy", preds.cpu().numpy())
        n_batch += 1
        time.sleep(random.uniform(2, 10))   # menunggu batch berikutnya

    print(f"[benign_batch_inference] selesai, {n_batch} batch.")


register(Scenario(
    name="benign_batch_inference",
    label="benign",
    script_path="scenarios/benign_batch_inference.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=None,
    description="Burst GPU per batch dengan jeda 2-10 s tidak teratur, "
                "I/O tulis hasil prediksi per batch.",
    queue_waiters=(0, 1),
))


if __name__ == "__main__":
    main()
