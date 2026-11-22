"""
scenarios/benign_model_resident_notebook.py
=============================================
Job BENIGN: notebook dengan model besar tertahan di VRAM, misalnya
eksperimen inferensi atau evaluasi model bahasa. Model dimuat sekali
(VRAM 50-85% kuota sesi, sama dengan R1), lalu pengguna menjalankan cell
bergantian, diselingi jeda berpikir acak:

  infer    forward pass model atas batch berukuran acak; memakai seluruh
           bobot, jadi utilisasi GPU dan utilisasi memori GPU naik;
  cpu      olah hasil di CPU (numpy);
  inspect  cell singkat (melihat output) tanpa komputasi berarti.

Cell pertama selalu infer (uji model setelah dimuat), dan cell berikutnya
pasti infer bila model sudah MODEL_GAP_SEC tidak dipakai: pengguna aktif
memakai modelnya secara berkala. Tanpa aturan ini, simulasi jadwal cell
menunjukkan 31% sesi 90 s (12% sesi 150 s) tidak memakai model lagi setelah
20 s pertama, sehingga tampak persis seperti R1. Aktivitas notebook dicatat
di awal dan akhir tiap cell, seperti Jupyter memperbarui last_activity saat
cell dieksekusi dan selesai.

Pembanding sulit (hard negative) untuk R1, terutama
misuse_disguised_hoarding: VRAM sama besar, ada antrean, dan GPU idle di
sela cell. Bedanya, tiap cell GPU memakai model yang ditahan, besar
komputasinya bervariasi, dan aktivitas notebook selalu menyertai cell.
Menutup celah dataset v3: VRAM benign maksimum ~3 GB, sedangkan R1
menahan 4-9 GB, sehingga VRAM saja cukup untuk mengenali R1.

Penyederhanaan: bobot dibuat acak langsung di GPU, tanpa membaca checkpoint
dari disk (R1 juga mengalokasikan memorinya tanpa disk). Jeda berpikir
selalu >= rata-rata lama cell, agar sesi tidak memenuhi pola R7 (CPU penuh
karena spin-wait sinkronisasi, GPU rata-rata rendah).
"""
import argparse
import random
import time

import numpy as np
import torch

from scenario_base import LogWriter, Scenario, add_common_args, quota_frac, touch_activity
from registry import register

CELL_WEIGHTS = {"infer": 0.6, "cpu": 0.25, "inspect": 0.15}
MODEL_GAP_SEC = 30   # cell berikutnya pasti infer bila model selama ini tidak dipakai


def build_model(model_mem_mb, width, device):
    """Tumpukan lapisan width x width float32 sebesar ~model_mem_mb."""
    layer_mb = width * width * 4 / 2 ** 20
    n_layers = max(1, int(model_mem_mb // layer_mb))
    return [torch.randn(width, width, device=device).mul_(width ** -0.5)
            for _ in range(n_layers)]


def forward(layers, batch, device):
    x = torch.randn(batch, layers[0].shape[0], device=device)
    for w in layers:
        x = torch.tanh(x @ w)
    return x


def run_cell(cell, layers, device, seconds):
    end = time.time() + seconds
    if cell == "infer":
        batch = random.choice([1, 4, 16, 64])
        while time.time() < end:
            forward(layers, batch, device).sum().item()   # .item() = menunggu hasil
    elif cell == "cpu":
        while time.time() < end:
            data = np.random.randn(100_000, 8)
            _ = np.percentile(data, [5, 50, 95], axis=0)
    else:
        time.sleep(random.uniform(0.5, 2.0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--model-mem-mb", type=int, default=2048)
    ap.add_argument("--width", type=int, default=4096, help="lebar lapisan model")
    ap.add_argument("--max-think-sec", type=float, default=60,
                    help="jeda berpikir maksimum antar cell (di bawah ambang idle)")
    ap.add_argument("--max-cell-sec", type=float, default=15,
                    help="lama maksimum satu cell infer/cpu")
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    touch_activity()   # cell memuat model
    layers = build_model(args.model_mem_mb, args.width, device)

    # Cell dan jeda dipotong ke sisa durasi: tanpa ini sesi bisa molor hingga
    # max_think + max_cell (110 s), melewati timeout runner (durasi + 60 s).
    end = time.time() + args.duration_sec
    n_cells = {name: 0 for name in CELL_WEIGHTS}
    last_infer = None
    while time.time() < end:
        if last_infer is None or time.time() - last_infer >= MODEL_GAP_SEC:
            cell = "infer"   # uji model setelah dimuat / pemakaian berkala
        else:
            cell = random.choices(list(CELL_WEIGHTS), weights=list(CELL_WEIGHTS.values()))[0]
        touch_activity()
        run_cell(cell, layers, device,
                 min(random.uniform(2, args.max_cell_sec), max(1.0, end - time.time())))
        touch_activity()
        if cell == "infer":
            last_infer = time.time()
        n_cells[cell] += 1
        log.tick()
        time.sleep(max(0.0, min(random.uniform(5, args.max_think_sec), end - time.time())))

    print(f"[benign_model_resident_notebook] selesai: {len(layers)} lapisan, cell {n_cells}.")


register(Scenario(
    name="benign_model_resident_notebook",
    label="benign",
    script_path="scenarios/benign_model_resident_notebook.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=None,
    description="Model besar tertahan di VRAM (50-85% kuota, seperti R1); cell "
                "inferensi/CPU/inspeksi memakai model itu, diselingi jeda berpikir.",
    params={
        "duration_sec": (180, 600),
        # sama dengan R1: VRAM besar bukan penanda misuse
        "model_mem_mb": quota_frac(0.5, 0.85),
        "width": [2048, 4096, 8192],
        # rata-rata jeda berpikir (>= 17,5 s) >= rata-rata lama cell (<= 11 s)
        "max_think_sec": (30, 90),     # tetap di bawah ambang idle 120 s
        "max_cell_sec": (5, 20),
    },
    queue_waiters=(1, 3),     # sama dengan R1: antrean bukan penanda misuse
    sibling_sessions=(0, 1),
))


if __name__ == "__main__":
    main()
