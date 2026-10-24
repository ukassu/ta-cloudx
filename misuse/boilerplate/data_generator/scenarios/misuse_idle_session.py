"""
scenarios/misuse_idle_session.py
===================================
Job MISUSE R2 (K2): idle session abuse. Pengguna aktif menjalankan
beberapa cell di awal sesi, lalu meninggalkan notebook: kernel idle,
tidak ada aktivitas, tetapi sesi (dan GPU yang dialokasikan) tetap
dipegang melewati ambang idle. Beda dengan R1: memori GPU kecil
(hanya model kecil + CUDA context), signature utamanya
last_activity_age_sec yang terus naik.
"""
import argparse
import random
import time

import torch

from scenario_base import Scenario, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=900)
    ap.add_argument("--active-fraction", type=float, default=0.15,
                    help="porsi awal sesi saat pengguna masih aktif")
    args = ap.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = torch.nn.Linear(1024, 1024).to(device)

    start = time.time()
    active_until = start + args.duration_sec * args.active_fraction
    while time.time() < active_until:
        touch_activity()
        burst_end = time.time() + random.uniform(2, 8)
        while time.time() < burst_end:
            model(torch.randn(256, 1024, device=device)).sum().backward()
        time.sleep(random.uniform(3, 15))

    # notebook ditinggal: kernel idle, sesi tetap hidup
    while time.time() - start < args.duration_sec:
        time.sleep(5)

    print("[misuse_idle_session] selesai.")


register(Scenario(
    name="misuse_idle_session",
    label="misuse",
    script_path="scenarios/misuse_idle_session.py",
    default_args=["--duration-sec", "900"],
    timeout_sec=920,
    description="Aktif di awal (~15% sesi), lalu kernel idle tanpa aktivitas; "
                "GPU tetap teralokasi, last activity melewati ambang idle.",
    code="R2",
    label_class="K2",
    queue_waiters=(0, 1),
))


if __name__ == "__main__":
    main()
