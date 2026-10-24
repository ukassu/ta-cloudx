"""
scenarios/misuse_quota_exceed.py
===================================
Job MISUSE: melebihi kuota resmi yang seharusnya dipatuhi. Berbeda
dari skenario lain di folder ini, skenario ini menghasilkan METADATA
TAMBAHAN (claimed_quota_mb) yang perlu ditangkap runner/telemetry
logger di luar kolom numerik standar -- lihat catatan `extra_meta` di
job_runner.py.
"""
import argparse
import time

import torch

from scenario_base import Scenario, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=300)
    ap.add_argument("--claimed-quota-mb", type=int, default=512)
    # 4x kuota; 2048 MB agar muat di node WSL (~3.7 GB RAM bebas)
    ap.add_argument("--actual-alloc-mb", type=int, default=2048)
    args = ap.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    # SENGAJA tidak membatasi diri ke claimed_quota_mb -- mensimulasikan
    # proses yang bypass mekanisme quota enforcement.
    n_floats = int(args.actual_alloc_mb * 1024 * 1024 / 4)
    try:
        # zeros (bukan empty): di CPU, empty tidak menyentuh halaman memori
        _held = torch.zeros(n_floats, device=device)
        bypass_success = True
    except RuntimeError:
        bypass_success = False

    a = torch.randn(1024, 1024, device=device)
    b = torch.randn(1024, 1024, device=device)
    start = last_touch = time.time()
    touch_activity()
    while time.time() - start < args.duration_sec:
        _ = a @ b
        time.sleep(0.5)
        if time.time() - last_touch >= 10:
            touch_activity()
            last_touch = time.time()

    print(f"[misuse_quota_exceed] claimed={args.claimed_quota_mb}MB "
          f"actual={args.actual_alloc_mb}MB bypass_success={bypass_success}")


register(Scenario(
    name="misuse_quota_exceed",
    label="misuse",
    script_path="scenarios/misuse_quota_exceed.py",
    default_args=["--duration-sec", "300", "--claimed-quota-mb", "512",
                    "--actual-alloc-mb", "2048"],
    timeout_sec=320,
    description="Alokasi GPU memory jauh melebihi claimed_quota_mb (kolom CSV).",
    code="R3",
    label_class="K2",
    extra_meta={"claimed_quota_mb": 512},
))


if __name__ == "__main__":
    main()
