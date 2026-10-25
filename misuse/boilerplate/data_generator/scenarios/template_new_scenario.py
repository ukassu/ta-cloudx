"""
scenarios/template_new_scenario.py
=====================================
TEMPLATE untuk menambah skenario benign/misuse BARU. Salin file ini,
ganti nama file & isi sesuai kebutuhan, dan skenario Anda otomatis
terdeteksi oleh registry.discover_scenarios() -- TIDAK ADA file lain
yang perlu diubah. (File berawalan "template_" sendiri tidak ikut
dijalankan.)

Langkah:
  1. Salin file ini -> scenarios/misuse_nama_skenario_anda.py
  2. Ganti fungsi main() dengan logika job yang merepresentasikan
     pola resource yang ingin Anda simulasikan. Panggil
     touch_activity() saat "pengguna menjalankan cell" dan log.tick()
     di dalam loop.
  3. Setiap argumen CLI yang ingin divariasikan per sesi ditambahkan ke
     `params` dengan nama yang sama (duration_sec <-> --duration-sec).
  4. Isi register(Scenario(...)) di bagian bawah dengan metadata yang
     BENAR -- terutama `label`, `code`, `label_class` (Kajian CATS
     Bagian 2-4), dan `params` (wajib memuat duration_sec).
  5. Uji jalankan standalone dulu: `python scenarios/misuse_nama_anda.py --duration-sec 10`
  6. Jalankan run_experiment.py -- skenario baru otomatis ikut
     terpanggil tanpa konfigurasi tambahan.

Contoh ide skenario misuse lain yang BELUM ada di boilerplate ini,
sebagai referensi (hapus/ganti sesuai kebutuhan riset Anda):
  - Bursty/intermittent abuse: job yang aktif sangat singkat lalu idle
    lama berulang-ulang, mencoba tetap di bawah radar rata-rata window.
  - P6 session recycling: sesi baru dibuat berulang untuk menghindari
    batas waktu (butuh dukungan runner untuk sesi berantai).
"""
import argparse
import time

# import torch  # jika skenario Anda perlu GPU

from scenario_base import LogWriter, Scenario, add_common_args, mem_frac, touch_activity
from registry import register


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration-sec", type=int, default=600)
    ap.add_argument("--hold-mem-mb", type=int, default=512)
    # TODO: tambahkan argumen lain sesuai kebutuhan skenario Anda
    add_common_args(ap)
    args = ap.parse_args()
    log = LogWriter(args.log_every_sec, args.log_kb)

    touch_activity()
    start = time.time()
    while time.time() - start < args.duration_sec:
        # TODO: implementasikan pola resource yang ingin disimulasikan
        # di sini -- ini bagian INTI yang menentukan signature
        # telemetri skenario Anda (lihat contoh-contoh skenario lain
        # di folder ini untuk pola: konstan, siklik, idle, dsb).
        log.tick()
        time.sleep(1)

    print("[template_new_scenario] selesai.")


register(Scenario(
    name="TODO_ganti_nama_unik",       # WAJIB unik di antara semua skenario
    label="misuse",                     # "benign" atau "misuse"
    script_path="scenarios/TODO_nama_file_ini.py",
    default_args=["--duration-sec", "600"],
    timeout_sec=620,                    # dipakai bila dijalankan tanpa run_experiment
    description="TODO: jelaskan pola telemetri yang diharapkan dari skenario ini.",
    code="TODO",                        # mis. "R1"; benign: hapus code & label_class
    label_class="K2",
    params={
        "duration_sec": (180, 600),     # (lo, hi) uniform
        "hold_mem_mb": mem_frac(0.1, 0.3),  # porsi memori GPU node
        # "mode": ["a", "b"],           # [..] pilih salah satu
    },
))


if __name__ == "__main__":
    main()
