"""
scenarios/misuse_synthetic_load.py
=====================================
Job MISUSE: beban komputasi konstan sintetis, PROXY untuk pola
cryptomining (bukan software mining sungguhan). Signature: GPU
utilization tinggi & konstan tanpa fase idle/checkpoint.
"""
import argparse

import torch

from scenario_base import Scenario, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=1800)
    ap.add_argument("--matrix-size", type=int, default=4096)
    args = ap.parse_args()

    # Satu kali eksekusi cell untuk meluncurkan beban, lalu notebook
    # ditinggal -- beban berjalan terus tanpa aktivitas notebook.
    touch_activity()
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    a = torch.randn(args.matrix_size, args.matrix_size, device=device)
    b = torch.randn(args.matrix_size, args.matrix_size, device=device)

    import time
    start = time.time()
    while time.time() - start < args.duration_sec:
        _ = a @ b
        if torch.cuda.is_available():
            torch.cuda.synchronize()

    print("[misuse_synthetic_load] selesai (beban konstan, tanpa siklus).")


register(Scenario(
    name="misuse_synthetic_load",
    label="misuse",
    script_path="scenarios/misuse_synthetic_load.py",
    default_args=["--duration-sec", "1800"],
    timeout_sec=1830,
    description="GPU utilization tinggi & flat terus-menerus, tanpa I/O, "
                "aktivitas notebook hanya di awal (proxy cryptomining).",
    code="S1",
    label_class="K3",
))


if __name__ == "__main__":
    main()
