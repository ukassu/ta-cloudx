"""
scenarios/misuse_inefficient_gpu.py
======================================
Job MISUSE R7 (K1): inefficient GPU workload -- bukan pelanggaran,
tetapi GPU terbuang karena bottleneck. Batch disiapkan dengan loop
Python satu thread (preprocessing per sampel) sehingga GPU lebih banyak
menunggu: utilisasi GPU rendah, CPU ~1 core penuh, progres training
tetap ada. Pembanding penting untuk R1 (sama-sama GPU rendah, tetapi
CPU tinggi dan aktivitas ada).
"""
import argparse
import time

import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim

from scenario_base import Scenario, touch_activity
from registry import register


def slow_preprocess(n, size=32):
    batch = np.empty((n, 3, size, size), dtype=np.float32)
    for i in range(n):   # sengaja per sampel, satu thread
        img = np.random.rand(3, size, size).astype(np.float32)
        for c in range(3):
            img[c] = (img[c] - img[c].mean()) / (img[c].std() + 1e-6)
        batch[i] = img[:, :, ::-1]
    return batch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--batch-size", type=int, default=256)
    args = ap.parse_args()

    torch.set_num_threads(1)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = nn.Sequential(
        nn.Conv2d(3, 32, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        nn.Flatten(), nn.Linear(32 * 16 * 16, 10),
    ).to(device)
    opt = optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.CrossEntropyLoss()

    start = time.time()
    step = 0
    while time.time() - start < args.duration_sec:
        x = torch.from_numpy(slow_preprocess(args.batch_size)).to(device)
        y = torch.randint(0, 10, (args.batch_size,), device=device)
        opt.zero_grad()
        loss_fn(model(x), y).backward()
        opt.step()
        step += 1
        if step % 10 == 0:
            touch_activity()

    print(f"[misuse_inefficient_gpu] selesai, {step} step.")


register(Scenario(
    name="misuse_inefficient_gpu",
    label="misuse",
    script_path="scenarios/misuse_inefficient_gpu.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=620,
    description="GPU util rendah karena preprocessing Python satu thread; "
                "CPU ~100% (1 core), progres training tetap ada.",
    code="R7",
    label_class="K1",
    queue_waiters=(0, 2),
))


if __name__ == "__main__":
    main()
