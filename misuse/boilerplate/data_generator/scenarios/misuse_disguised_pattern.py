"""
scenarios/misuse_disguised_pattern.py
========================================
Job MISUSE: beban GPU diprogram naik-turun mengikuti pola sinusoidal,
MENIRU siklus training job normal secara rata-rata statis, tapi
periodisitasnya terlalu reguler & tanpa fase I/O -- paling sulit
dibedakan dari job benign dengan fitur agregat statis saja, sehingga
cocok untuk menguji representasi temporal (feature_mode="sequence").
"""
import argparse
import math
import time

import torch

from scenario_base import Scenario, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=1800)
    ap.add_argument("--period-sec", type=int, default=60)
    ap.add_argument("--matrix-size", type=int, default=2048)
    args = ap.parse_args()

    touch_activity()  # hanya saat diluncurkan
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    a = torch.randn(args.matrix_size, args.matrix_size, device=device)
    b = torch.randn(args.matrix_size, args.matrix_size, device=device)

    start = time.time()
    while time.time() - start < args.duration_sec:
        elapsed = time.time() - start
        phase = (elapsed % args.period_sec) / args.period_sec
        intensity = 0.2 + 0.8 * (0.5 + 0.5 * math.sin(2 * math.pi * phase))
        n_iters = max(1, int(intensity * 5))
        for _ in range(n_iters):
            _ = a @ b
        if torch.cuda.is_available():
            torch.cuda.synchronize()
        time.sleep(0.5)

    print("[misuse_disguised_pattern] selesai.")


register(Scenario(
    name="misuse_disguised_pattern",
    label="misuse",
    script_path="scenarios/misuse_disguised_pattern.py",
    default_args=["--duration-sec", "1800", "--period-sec", "60"],
    timeout_sec=1830,
    description="Osilasi GPU mirip siklus training, tapi periodisitas terlalu reguler, "
                "tanpa I/O (cryptomining tersembunyi).",
    code="S1",
    label_class="K3",
))


if __name__ == "__main__":
    main()
