"""
scenarios/misuse_gpu_monopolization.py
=========================================
Job MISUSE P5 (K2): resource monopolization. Workload terlihat sah
(training intensif, aktivitas notebook ada), tetapi memegang GPU lebih
lama dari kebanyakan sesi lain sementara job pengguna lain terus
berdatangan dan menunggu -- antrean dan waktu tunggu naik sepanjang sesi
(queue_arrival="spread"). Durasinya tumpang-tindih dengan training
benign yang panjang, jadi pembedanya kontekstual: durasi + antrean +
waktu tunggu, bukan utilisasi semata.
"""
import argparse
import time

import torch
import torch.nn as nn
import torch.optim as optim

from scenario_base import LogWriter, Scenario, add_common_args, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=1800)
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--width", type=int, default=64)
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    w = args.width
    model = nn.Sequential(
        nn.Conv2d(3, w, 3, padding=1), nn.ReLU(),
        nn.Conv2d(w, w * 2, 3, padding=1), nn.ReLU(), nn.MaxPool2d(2),
        nn.Flatten(), nn.Linear(w * 2 * 16 * 16, 10),
    ).to(device)
    opt = optim.Adam(model.parameters(), lr=1e-3)
    loss_fn = nn.CrossEntropyLoss()

    start = time.time()
    epoch = 0
    while time.time() - start < args.duration_sec:
        for _ in range(200):
            x = torch.randn(args.batch_size, 3, 32, 32, device=device)
            y = torch.randint(0, 10, (args.batch_size,), device=device)
            opt.zero_grad()
            loss_fn(model(x), y).backward()
            opt.step()
        touch_activity()
        log.tick()
        epoch += 1

    print(f"[misuse_gpu_monopolization] selesai, {epoch} epoch.")


register(Scenario(
    name="misuse_gpu_monopolization",
    label="misuse",
    script_path="scenarios/misuse_gpu_monopolization.py",
    default_args=["--duration-sec", "1800"],
    timeout_sec=1830,
    description="Training intensif yang memegang GPU lama sementara antrean "
                "& waktu tunggu terus naik.",
    code="P5",
    label_class="K2",
    params={
        "duration_sec": (600, 1200),
        "batch_size": [64, 128, 256],
        "width": [32, 64, 128],
    },
    duration_scale=3.0,
    queue_waiters=(2, 4),
    queue_arrival="spread",
))


if __name__ == "__main__":
    main()
