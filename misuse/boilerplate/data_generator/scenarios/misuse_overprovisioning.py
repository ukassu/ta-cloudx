"""
scenarios/misuse_overprovisioning.py
=======================================
Job MISUSE R4 (K1): resource overprovisioning -- inefisien tetapi sah.
Container meminta CPU/RAM jauh di atas kebutuhan (request_profiles ->
prom_cpu_request_cores, prom_mem_request_mb), sementara pemakaian
aktual kecil (olah data ringan + sesekali operasi GPU kecil). Signature:
rasio requested vs used yang besar. Sesi lain juga punya request (profil
default), jadi pembedanya besar request, bukan ada/tidaknya.
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
    ap.add_argument("--pause-sec", type=float, default=5)
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    torch.set_num_threads(1)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    start = time.time()
    while time.time() - start < args.duration_sec:
        touch_activity()
        data = np.random.randn(50_000, 8)
        _ = data.mean(axis=0), data.std(axis=0)
        if random.random() < 0.3:
            _ = torch.randn(256, 256, device=device) @ torch.randn(256, 256, device=device)
        log.tick()
        time.sleep(random.uniform(1, 2 * args.pause_sec))

    print("[misuse_overprovisioning] selesai.")


register(Scenario(
    name="misuse_overprovisioning",
    label="misuse",
    script_path="scenarios/misuse_overprovisioning.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=620,
    description="Request 4-8 CPU / 3-4 GiB tetapi pemakaian < 0.5 core / ~300 MB.",
    code="R4",
    label_class="K1",
    params={
        "duration_sec": (180, 600),
        "pause_sec": (2, 10),
    },
    request_profiles=[
        {"cpu": "4", "memory": "3Gi"},
        {"cpu": "6", "memory": "4Gi"},
        {"cpu": "8", "memory": "4Gi"},
    ],
))


if __name__ == "__main__":
    main()
