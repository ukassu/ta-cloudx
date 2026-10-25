"""
scenarios/misuse_synthetic_load.py
=====================================
Job MISUSE S1 (K3): beban komputasi konstan sintetis, PROXY untuk pola
cryptomining (bukan software mining sungguhan). Signature: GPU
utilization tinggi & konstan tanpa fase idle/checkpoint. `--duty`
meniru miner yang men-throttle diri agar tidak terlalu mencolok.
"""
import argparse
import time

import torch

from scenario_base import LogWriter, Scenario, add_common_args, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=1800)
    ap.add_argument("--matrix-size", type=int, default=4096)
    ap.add_argument("--duty", type=float, default=1.0,
                    help="porsi waktu sibuk per siklus 1 s (1.0 = penuh)")
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    # Satu kali eksekusi cell untuk meluncurkan beban, lalu notebook
    # ditinggal -- beban berjalan terus tanpa aktivitas notebook.
    touch_activity()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    a = torch.randn(args.matrix_size, args.matrix_size, device=device)
    b = torch.randn(args.matrix_size, args.matrix_size, device=device)

    start = time.time()
    while time.time() - start < args.duration_sec:
        busy_until = time.time() + args.duty
        while time.time() < busy_until:
            _ = a @ b
            if torch.cuda.is_available():
                torch.cuda.synchronize()
        if args.duty < 1.0:
            time.sleep(1.0 - args.duty)
        log.tick()

    print("[misuse_synthetic_load] selesai (beban konstan, tanpa siklus).")


register(Scenario(
    name="misuse_synthetic_load",
    label="misuse",
    script_path="scenarios/misuse_synthetic_load.py",
    default_args=["--duration-sec", "1800"],
    timeout_sec=1830,
    description="GPU utilization tinggi & flat terus-menerus, "
                "aktivitas notebook hanya di awal (proxy cryptomining).",
    code="S1",
    label_class="K3",
    params={
        "duration_sec": (240, 600),
        "matrix_size": [2048, 3072, 4096],
        "duty": (0.7, 1.0),
    },
))


if __name__ == "__main__":
    main()
