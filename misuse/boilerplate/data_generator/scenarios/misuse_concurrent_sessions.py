"""
scenarios/misuse_concurrent_sessions.py
==========================================
Job MISUSE R6 (K2): excessive concurrent sessions. Sesi utama terlihat
wajar (notebook GPU interaktif), tetapi pengguna yang sama membuka
beberapa sesi notebook lain secara bersamaan -- runner menjalankannya
sebagai Pod sibling (sibling_sessions) sehingga kolom
prom_user_active_sessions melampaui batas kebijakan (policy.json).
Sesuai dokumen, R6 dideteksi berbasis aturan; skenario ini menjadi
baseline/sanity check.
"""
import argparse
import random
import time

import torch

from scenario_base import LogWriter, Scenario, add_common_args, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--max-think-sec", type=float, default=40)
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = torch.nn.Sequential(
        torch.nn.Linear(1024, 2048), torch.nn.ReLU(), torch.nn.Linear(2048, 10),
    ).to(device)

    start = time.time()
    while time.time() - start < args.duration_sec:
        touch_activity()
        burst_end = time.time() + random.uniform(3, 12)
        while time.time() < burst_end:
            model(torch.randn(512, 1024, device=device)).sum().backward()
        log.tick()
        time.sleep(random.uniform(5, args.max_think_sec))

    print("[misuse_concurrent_sessions] selesai.")


register(Scenario(
    name="misuse_concurrent_sessions",
    label="misuse",
    script_path="scenarios/misuse_concurrent_sessions.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=620,
    description="Sesi utama wajar, tetapi 2-4 sesi notebook lain milik pengguna "
                "yang sama berjalan bersamaan (3-5 sesi > batas 2).",
    code="R6",
    label_class="K2",
    params={
        "duration_sec": (180, 600),
        "max_think_sec": (10, 60),
    },
    queue_waiters=(0, 1),
    sibling_sessions=(2, 4),
))


if __name__ == "__main__":
    main()
