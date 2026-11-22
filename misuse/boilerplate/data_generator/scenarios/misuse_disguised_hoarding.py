"""
scenarios/misuse_disguised_hoarding.py
=========================================
Job MISUSE R1 (K2), varian MENYAMAR: GPU hoarding yang berusaha terlihat
seperti notebook interaktif yang sedang dipakai. VRAM ditahan 50-85% kuota
sesi tanpa pekerjaan berguna; yang ada hanya kosmetik:

  - touch_activity() berkala: memalsukan last_activity agar sesi tidak
    di-cull idle culler;
  - burst kernel GPU berkala: memalsukan utilisasi GPU. Meniru KeepGPU
    (pypi keep-gpu), yang menahan tensor di VRAM lalu menjalankan burst
    operasi CUDA berkala agar GPU tidak direklamasi scheduler. Dua mode:
      matmul  perkalian matriks acak yang tidak menyentuh memori ditahan;
      held    operasi elementwise pada memori yang ditahan, sehingga
              utilisasi memori GPU ikut naik seperti saat model dipakai;
  - opsional, kerja CPU numpy beberapa detik tiap aktivitas (memalsukan
    cell yang dieksekusi).

Beda dengan misuse_idle_hoarding (R1 polos): di sana GPU benar-benar diam.
Pembanding benign: benign_model_resident_notebook (VRAM sama besar, cell
memakai model yang ditahan). Yang membedakan dari benign: aktivitas dan
burst dijadwalkan terpisah (tidak saling terkait), burst tidak memakai
model, ukurannya seragam, dan GPU idle sebagian besar waktu sementara
antrean > 0. Porsi waktu burst dibatasi <= ~20% agar rata-rata utilisasi
tetap rendah (definisi R1); cek ground truth-nya ada di
validate_dataset.py (GT_CHECKS_BY_JOB).

Tingkat kesulitan: `jitter` (0 = jeda dan lama burst teratur, mudah
dikenali; 1 = acak seperti pengguna), `burst_sec` / `kernel_every_sec` /
`kernel_size` (besar utilisasi palsu), `burst_mode`, dan `fake_cpu_sec`.
Semuanya tercatat sebagai kolom param_* (ground truth, BUKAN fitur model).
`kernel_size` hanya dipakai mode matmul.
"""
import argparse
import random
import time

import numpy as np
import torch

from scenario_base import LogWriter, Scenario, add_common_args, quota_frac, touch_activity
from registry import register

# Selang maksimum antar-aktivitas. Ditambah burst atau kerja CPU yang sedang
# berjalan (<= 6 s) dan keterlambatan sampel (1 interval), tetap di bawah
# idle_timeout_sec 120 s di policy.json: sesi ini TIDAK boleh menjadi R2.
MAX_ACTIVITY_GAP_SEC = 90
MIN_GAP_SEC = 2
CPU_MODE_MAX_KERNEL = 512   # matmul 1 thread di CPU: burst tetap pendek


def next_gap(mean_sec, jitter, cap=None):
    """Jeda sampai kejadian berikutnya. jitter 0 -> tepat mean_sec (timer
    teratur); jitter 1 -> acak 0,2x-1,8x mean_sec (rata-rata tetap mean_sec)."""
    gap = max(MIN_GAP_SEC, mean_sec * random.uniform(1 - 0.8 * jitter, 1 + 0.8 * jitter))
    return min(gap, cap) if cap else gap


def burst_length(burst_sec, jitter):
    """Lama satu burst: seragam saat jitter 0, 0,5x-1,5x saat jitter 1."""
    return burst_sec * random.uniform(1 - 0.5 * jitter, 1 + 0.5 * jitter)


def fake_burst(mode, held, a, b, seconds):
    """Kernel tanpa pekerjaan berguna selama `seconds` detik."""
    end = time.time() + seconds
    while time.time() < end:
        if mode == "held":
            held.add_(1.0)
        else:
            _ = a @ b
        if held.is_cuda:
            torch.cuda.synchronize()


def fake_cpu(seconds):
    """Kerja CPU numpy yang hasilnya dibuang (cell palsu)."""
    end = time.time() + seconds
    while time.time() < end:
        _ = np.sort(np.random.rand(200_000))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--hold-mem-mb", type=int, default=2048)
    ap.add_argument("--activity-every-sec", type=float, default=30,
                    help="rata-rata jeda touch_activity() (dibatasi < ambang idle)")
    ap.add_argument("--kernel-every-sec", type=float, default=30,
                    help="rata-rata jeda antar burst (terpisah dari aktivitas)")
    ap.add_argument("--burst-sec", type=float, default=2.0)
    ap.add_argument("--burst-mode", choices=["matmul", "held"], default="matmul")
    ap.add_argument("--kernel-size", type=int, default=2048, help="sisi matriks mode matmul")
    ap.add_argument("--jitter", type=float, default=0.5,
                    help="0 = jeda dan lama burst teratur, 1 = acak")
    ap.add_argument("--fake-cpu-sec", type=float, default=0.0,
                    help="detik kerja CPU numpy tiap aktivitas (0 = tidak ada)")
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    kernel_size = args.kernel_size
    if device.type == "cpu":
        torch.set_num_threads(1)
        kernel_size = min(kernel_size, CPU_MODE_MAX_KERNEL)
    # zeros (bukan empty): di CPU, empty tidak menyentuh halaman memori
    # sehingga RSS/working set tidak naik dan signature hilang.
    held = torch.zeros(int(args.hold_mem_mb * 1024 * 1024 / 4), device=device)
    a = b = None
    if args.burst_mode == "matmul":
        a = torch.randn(kernel_size, kernel_size, device=device)
        b = torch.randn(kernel_size, kernel_size, device=device)

    start = time.time()
    next_activity = start
    next_burst = start + next_gap(args.kernel_every_sec, args.jitter)
    n_activity = n_burst = 0
    while time.time() - start < args.duration_sec:
        now = time.time()
        if now >= next_activity:
            touch_activity()
            if args.fake_cpu_sec > 0:
                fake_cpu(args.fake_cpu_sec)
            n_activity += 1
            next_activity = time.time() + next_gap(
                args.activity_every_sec, args.jitter, cap=MAX_ACTIVITY_GAP_SEC)
        if now >= next_burst:
            fake_burst(args.burst_mode, held, a, b, burst_length(args.burst_sec, args.jitter))
            n_burst += 1
            next_burst = time.time() + next_gap(args.kernel_every_sec, args.jitter)
        log.tick()
        time.sleep(0.5)

    print(f"[misuse_disguised_hoarding] selesai: {n_activity} aktivitas palsu, "
          f"{n_burst} burst {args.burst_mode}, VRAM ditahan tanpa pekerjaan berguna.")


register(Scenario(
    name="misuse_disguised_hoarding",
    label="misuse",
    script_path="scenarios/misuse_disguised_hoarding.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=620,  # tidak berhenti sendiri lebih awal dari durasi
    description="VRAM 50-85% kuota ditahan; aktivitas notebook dan burst kernel "
                "dipalsukan berkala agar terlihat dipakai, tanpa pekerjaan nyata; "
                "antrean > 0.",
    code="R1",
    label_class="K2",
    params={
        "duration_sec": (240, 600),
        # sama dengan R1 polos dan benign_model_resident_notebook: di bawah
        # kuota sesi sendiri (bukan R3), VRAM bukan penanda label
        "hold_mem_mb": quota_frac(0.5, 0.85),
        "activity_every_sec": (10, 60),    # selalu < ambang idle 120 s
        # porsi waktu burst maksimum 4 / (15 + 4) = 21%: utilisasi tetap rendah
        "kernel_every_sec": (15, 60),
        "burst_sec": (1.0, 4.0),
        "burst_mode": ["matmul", "held"],
        "kernel_size": [1024, 2048, 4096],
        "jitter": (0.0, 1.0),              # tingkat kesulitan penyamaran
        "fake_cpu_sec": (0.0, 2.0),
    },
    queue_waiters=(1, 3),
))


if __name__ == "__main__":
    main()
