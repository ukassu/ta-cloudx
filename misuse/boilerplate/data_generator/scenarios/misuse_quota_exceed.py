"""
scenarios/misuse_quota_exceed.py
===================================
Job MISUSE R3 (K2): melebihi kuota memori yang ditetapkan. Setiap sesi
punya kuota (kolom quota_mem_mb); skenario ini sengaja mengalokasikan
memori GPU 1.3-1.8x kuota tersebut, mensimulasikan proses yang
mem-bypass mekanisme quota enforcement.
"""
import argparse
import time

import torch

from scenario_base import LogWriter, Scenario, add_common_args, session_quota, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=300)
    ap.add_argument("--claimed-quota-mb", type=int, default=512)
    # 4x kuota; 2048 MB agar muat di node WSL (~3.7 GB RAM bebas)
    ap.add_argument("--actual-alloc-mb", type=int, default=2048)
    ap.add_argument("--matrix-size", type=int, default=1024)
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

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

    a = torch.randn(args.matrix_size, args.matrix_size, device=device)
    b = torch.randn(args.matrix_size, args.matrix_size, device=device)
    start = last_touch = time.time()
    touch_activity()
    while time.time() - start < args.duration_sec:
        _ = a @ b
        time.sleep(0.5)
        log.tick()
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
    description="Alokasi memori GPU 1.3-1.8x kuota sesi (quota_mem_mb).",
    code="R3",
    label_class="K2",
    params={
        "duration_sec": (180, 480),
        "claimed_quota_mb": session_quota(),
        # melampaui kuota, dibatasi 90% memori GPU agar tidak OOM
        "actual_alloc_mb": lambda rng, ctx: int(min(
            ctx["quota_mem_mb"] * rng.uniform(1.3, 1.8), 0.9 * ctx["mem_budget_mb"])),
        "matrix_size": [512, 1024, 2048],
    },
    queue_waiters=(0, 1),
))


if __name__ == "__main__":
    main()
