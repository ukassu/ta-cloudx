"""
scenarios/misuse_disguised_pattern.py
========================================
Job MISUSE S1 (K3): beban GPU diprogram naik-turun mengikuti pola
sinusoidal, MENIRU siklus training job normal secara rata-rata statis,
tapi periodisitasnya terlalu reguler & tanpa aktivitas notebook --
paling sulit dibedakan dari job benign dengan fitur agregat statis
saja, sehingga cocok untuk menguji representasi temporal
(feature_mode="sequence").
"""
import argparse
import math
import time

import torch

from scenario_base import LogWriter, Scenario, add_common_args, touch_activity
from registry import register


# Siklus beban jauh lebih pendek dari interval sampling 5 s. Intensitas =
# porsi waktu sibuk per siklus (bukan jumlah perkalian), sehingga utilisasi
# mengikuti gelombang di GPU lambat maupun cepat. Versi lama menghitung
# perkalian per siklus: di RTX 5060 Ti itu hanya ~1-17% GPU dan osilasinya hilang.
CYCLE_SEC = 0.5


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=1800)
    ap.add_argument("--period-sec", type=float, default=60)
    ap.add_argument("--matrix-size", type=int, default=2048)
    ap.add_argument("--min-intensity", type=float, default=0.2,
                    help="porsi waktu sibuk GPU di lembah gelombang")
    ap.add_argument("--max-intensity", type=float, default=1.0,
                    help="porsi waktu sibuk GPU di puncak gelombang")
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    touch_activity()  # hanya saat diluncurkan
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    a = torch.randn(args.matrix_size, args.matrix_size, device=device)
    b = torch.randn(args.matrix_size, args.matrix_size, device=device)

    lo, hi = args.min_intensity, args.max_intensity
    start = time.time()
    while time.time() - start < args.duration_sec:
        elapsed = time.time() - start
        phase = (elapsed % args.period_sec) / args.period_sec
        intensity = lo + (hi - lo) * (0.5 + 0.5 * math.sin(2 * math.pi * phase))
        busy_until = time.time() + intensity * CYCLE_SEC
        while time.time() < busy_until:
            _ = a @ b
            if torch.cuda.is_available():
                torch.cuda.synchronize()
        log.tick()
        time.sleep(max(0.0, (1 - intensity) * CYCLE_SEC))

    print("[misuse_disguised_pattern] selesai.")


register(Scenario(
    name="misuse_disguised_pattern",
    label="misuse",
    script_path="scenarios/misuse_disguised_pattern.py",
    default_args=["--duration-sec", "1800", "--period-sec", "60"],
    timeout_sec=1830,
    description="Osilasi GPU mirip siklus training, tapi periodisitas terlalu reguler "
                "(cryptomining tersembunyi).",
    code="S1",
    label_class="K3",
    params={
        "duration_sec": (240, 600),
        "period_sec": (30, 120),
        "matrix_size": [2048, 3072, 4096],
        "min_intensity": (0.1, 0.4),
        "max_intensity": (0.7, 1.0),
    },
    # cek pola_periodik butuh dua periode setelah fase pemanasan 20 s,
    # termasuk saat --duration-scale / --duration-sec memperpendek durasi
    min_duration_sec=lambda p, policy: 2 * p["period_sec"] + 30,
))


if __name__ == "__main__":
    main()
