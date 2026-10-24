"""
scenarios/misuse_idle_hoarding.py
====================================
Job MISUSE: GPU idle-hoarding. Mengalokasikan memori GPU lalu idle
dalam waktu lama tanpa komputasi nyata. Signature: GPU memory tinggi
konstan, GPU utilization mendekati 0%.
"""
import argparse
import time

import torch

from scenario_base import Scenario, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--hold-mem-mb", type=int, default=2048)
    args = ap.parse_args()

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    n_floats = int(args.hold_mem_mb * 1024 * 1024 / 4)
    # zeros (bukan empty): di CPU, empty tidak menyentuh halaman memori
    # sehingga RSS/working set tidak naik dan signature hilang.
    _held = torch.zeros(n_floats, device=device)

    # Pengguna sesekali membuka notebook (keep-alive) agar sesi tidak
    # di-cull idle culler, tapi GPU tidak dipakai untuk komputasi.
    touch_activity()
    keepalive_every = max(30, args.duration_sec // 4)
    start = last_touch = time.time()
    while time.time() - start < args.duration_sec:
        time.sleep(5)
        if time.time() - last_touch >= keepalive_every:
            touch_activity()
            last_touch = time.time()

    print("[misuse_idle_hoarding] selesai (GPU ditahan tanpa komputasi).")


register(Scenario(
    name="misuse_idle_hoarding",
    label="misuse",
    script_path="scenarios/misuse_idle_hoarding.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=620,  # tidak berhenti sendiri lebih awal dari durasi -> beri toleransi
    description="GPU memory tinggi konstan, utilization ~0% sepanjang sesi, "
                "sementara job lain antre (antrean > 0).",
    code="R1",
    label_class="K2",
    queue_waiters=(1, 3),
))


if __name__ == "__main__":
    main()
