# data_generator — Boilerplate Generator Dataset (Plugin Skenario)

Menghasilkan dataset telemetri berlabel (benign vs berbagai jenis
misuse) lewat sistem skenario yang bisa DIPERLUAS tanpa mengubah kode
inti (`job_runner.py`, `run_experiment.py`, `registry.py`).

## Struktur

```
data_generator/
├── scenario_base.py          # kontrak (dataclass) Scenario
├── registry.py                # plugin registry + auto-discovery
├── telemetry_logger.py        # perekam CPU/mem/GPU/IO -> CSV
├── job_runner.py              # jalankan 1 sesi dari Scenario + rekam
├── run_experiment.py          # orchestrator config-driven
└── scenarios/                  # 15 skenario, lihat tabel taksonomi di bawah
    └── template_new_scenario.py   # <- salin file ini untuk misuse baru (tidak ikut dijalankan)
```

## Skenario dan taksonomi CATS

Setiap skenario memetakan satu kode dari *Kajian Misuse Detection pada
CloudX (CATS) v5*, Bagian 2-4. Kolom `scenario_code` dan `label_class`
di dataset berasal dari sini; kolom `label` tetap biner (benign/misuse)
untuk detector.

| Skenario | Kode | Kelas | Pola simulasi | Konteks dari runner |
|----------|------|-------|---------------|---------------------|
| benign_cnn_training | BENIGN | - | GPU siklik per epoch, checkpoint | antrean 0-2 |
| benign_eda_notebook | BENIGN | - | CPU pandas, GPU ~0 | 2 sesi paralel (di bawah batas) |
| benign_interactive_gpu_notebook | BENIGN | - | burst per cell + jeda berpikir 5-60 s | antrean 0-1 |
| benign_data_pipeline_training | BENIGN | - | baca shard dari disk, dip GPU saat data loading | antrean 0-2 |
| benign_batch_inference | BENIGN | - | burst GPU per batch, I/O tulis | antrean 0-1 |
| misuse_idle_hoarding | R1 | K2 | VRAM ditahan, util ~0, keep-alive sesekali | antrean 1-3 |
| misuse_idle_session | R2 | K2 | aktif ~15% awal sesi lalu kernel idle | antrean 0-1 |
| misuse_quota_exceed | R3 | K2 | VRAM 4x `claimed_quota_mb` | - |
| misuse_overprovisioning | R4 | K1 | request 6 CPU / 4 GiB, pakai < 0.5 core | - |
| misuse_zombie_job | R5 | K2 | flat, tanpa aktivitas sejak awal | - |
| misuse_concurrent_sessions | R6 | K2 | sesi wajar + 3 sesi paralel (4 > batas 2) | 3 sibling |
| misuse_inefficient_gpu | R7 | K1 | preprocessing Python 1 thread, GPU rendah, CPU penuh | antrean 0-2 |
| misuse_gpu_monopolization | P5 | K2 | training intensif 3x lebih lama | antrean 2-4 bertahap |
| misuse_synthetic_load | S1 | K3 | GPU ~100% flat, tanpa aktivitas | - |
| misuse_disguised_pattern | S1 | K3 | osilasi GPU teratur, tanpa I/O | - |

Prioritas dokumen (R1, R2, R3, R6, P5, S1) tercakup; R4, R5, R7 (K1/K2)
disertakan sebagai pembanding sulit. Skenario benign sengaja diberi
antrean/sesi paralel kadang-kadang agar konteks bukan penanda label.

### Kolom konteks sesi dan cluster

| Kolom | Sumber | Keterangan |
|-------|--------|------------|
| `user_id` | run_experiment | pseudonim `u01`-`u08`, diundi per sesi |
| `scenario_code`, `label_class` | Scenario | kode taksonomi dan kelas K1/K2/K3 |
| `session_age_sec` | sidecar | umur proses job |
| `last_activity_age_sec` | sidecar | detik sejak `touch_activity()` terakhir (eksekusi cell) |
| `gpu_alloc_sec` | sidecar | detik-GPU teralokasi (0 di mode CPU) |
| `prom_queue_length` | Prometheus | Pod waiter GPU yang Pending (mode GPU) |
| `prom_queue_wait_max_sec` | Prometheus | waktu tunggu waiter terlama |
| `prom_user_active_sessions` | Prometheus | sesi utama + sibling yang Running |
| `claimed_quota_mb` | Scenario.extra_meta | hanya R3 |

Ambang kebijakan sandbox (idle, batas sesi, kuota) ditulis ke
`<out-dir>/policy.json` untuk baseline berbasis aturan.

Antrean dibuat dengan Pod "waiter" yang meminta `nvidia.com/gpu`; karena
GPU hanya satu dan sedang dipegang sesi, waiter Pending sehingga
antrean dan waktu tunggu di Prometheus nyata. Fitur ini hanya aktif di
mode GPU (`K8S_REQUEST_GPU=true`).

## Cara kerja sistem plugin

1. Tiap file di `scenarios/` berisi fungsi `main()` (job yang
   dijalankan sebagai proses) DAN satu baris `register(Scenario(...))`
   di bagian bawah file, dieksekusi saat modulnya di-import.
2. `registry.discover_scenarios()` meng-import SEMUA modul di
   `scenarios/` secara otomatis (pakai `pkgutil.iter_modules`), memicu
   efek samping `register()` di tiap file.
3. `run_experiment.py` memanggil `discover_scenarios()` lalu
   `all_scenarios()` untuk mendapat daftar lengkap — **tidak ada
   daftar skenario yang di-hardcode di file manapun**.

**Menambah jenis misuse baru = 1 langkah:** salin
`scenarios/template_new_scenario.py`, isi logika `main()` dan metadata
`register(Scenario(...))`, selesai. `run_experiment.py` langsung
mengikutsertakannya di run berikutnya.

## Instalasi

```bash
pip install torch psutil pandas numpy
```

## Menjalankan

```bash
# Jalankan SEMUA skenario terdaftar, 10 putaran
python run_experiment.py --repeats 10 --out-dir data

# Hanya skenario tertentu (mis. untuk uji cepat 1 skenario baru)
python run_experiment.py --repeats 2 --scenarios misuse_idle_hoarding

# Uji 1 skenario secara standalone (tanpa runner/telemetry)
python scenarios/misuse_idle_hoarding.py --duration-sec 30
```

Output: `data/<session_id>.csv` per sesi, digabung otomatis jadi
`data/dataset.csv` di akhir proses.

### Metrik Prometheus saat executor Kubernetes

Saat menjalankan `run_experiment.py --executor k8s`, sidecar merekam
telemetri proses dengan `psutil`. Setelah job selesai, runner (di host)
mengambil metrik Pod tersebut dari Prometheus lewat `query_range` untuk
rentang waktu job, dengan step = `--interval-sec`. Query dilakukan dari
host, bukan dari sidecar, supaya traffic query tidak ikut terhitung di
metrik network Pod. Runner menyimpan tiga hasil per sesi:

```text
<session_id>.csv             # telemetri lokal psutil
<session_id>.prometheus.csv  # telemetri dari Prometheus
<session_id>.merged.csv      # hasil penggabungan berdasarkan waktu
```

File `dataset.csv` hanya dibentuk dari file `*.merged.csv`, sehingga
data mentah tidak terhitung dua kali. URL Prometheus diakses dari host
(ingress), defaultnya:

```text
http://prometheus.127.0.0.1.sslip.io
```

Konfigurasi lewat environment variable:

| Variable | Default | Keterangan |
|----------|---------|------------|
| `PROMETHEUS_URL` | `http://prometheus.127.0.0.1.sslip.io` | URL Prometheus dari host |
| `PROM_RATE_WINDOW` | `1m` | jendela `rate()`; minimal 2x scrape interval cAdvisor (10s) |
| `PROM_SCRAPE_LAG_SEC` | `25` | jeda setelah job selesai sebelum query, agar scrape terakhir masuk |
| `K8S_REQUEST_GPU` | `false` | `true` = container job meminta `nvidia.com/gpu: 1`, sidecar membaca `nvidia-smi` |
| `JOB_IMAGE` | `local/data-generator-job:latest` | pakai `local/data-generator-job:gpu` (torch CUDA) saat mode GPU |

Image GPU dibangun dari Dockerfile yang sama:

```bash
docker build -f docker/Dockerfile.job \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu126 \
  -t local/data-generator-job:gpu .
```

File `.merged.csv` memiliki kolom lokal (`cpu_pct`, `mem_rss_mb`, dan
sebagainya), ditambah kolom Prometheus dengan prefix `prom_`. Metrik
CPU, memory, I/O, restart, dan request/limit hanya dari container
`job` (sidecar tidak ikut terhitung); metrik network bersifat per Pod.
Jika suatu metrik tidak tersedia, nilainya kosong; jika Prometheus
tidak dapat diakses, data `psutil` tetap tersimpan dan kolom `prom_*`
kosong.

Perbedaan semantik kolom lokal vs Prometheus:

| Lokal (psutil) | Prometheus | Catatan |
|----------------|------------|---------|
| `cpu_pct` (100 = 1 core, sesaat) | `prom_cpu_pct` (100 = 1 core, rata-rata `PROM_RATE_WINDOW`) | Prometheus lebih halus/tertinggal |
| `mem_rss_mb` (RSS proses) | `prom_mem_rss_mb` (working set container) | nilai mirip |
| `io_read_mb`, `io_write_mb` (**kumulatif** MB) | `prom_io_read_mb`, `prom_io_write_mb` (**laju** MB/s) | jangan dicampur |

## Menangani skenario dengan metadata tambahan (mis. kuota)

Skenario seperti `misuse_quota_exceed.py` butuh kolom tambahan di CSV
(`claimed_quota_mb`) yang tidak ada di skenario lain. Oper lewat
`extra_meta` saat memanggil `run_labeled_job()` langsung (bukan lewat
`run_experiment.py` generik, yang tidak tahu skenario mana butuh
metadata apa):

```python
from registry import discover_scenarios, get
from job_runner import run_labeled_job

discover_scenarios()
scenario = get("misuse_quota_exceed")
run_labeled_job(
    scenario, session_id="custom_001", out_dir="data",
    extra_meta={"claimed_quota_mb": 1024},
)
```

TODO (mahasiswa): jika BANYAK skenario butuh extra_meta yang berbeda-
beda, pertimbangkan menambah field `extra_meta_template: dict` di
`Scenario` (scenario_base.py) supaya `run_experiment.py` generik bisa
otomatis mengopernya tanpa kode khusus per skenario.

## Menjalankan di Kubernetes/kind (bukan lokal)

Boilerplate ini menjalankan skenario sebagai `subprocess` lokal
(`job_runner.py`). Untuk menjalankan di cluster kind, ganti
`job_runner.py` dengan versi yang submit Kubernetes Pod (2 container:
job + telemetry sidecar dengan `shareProcessNamespace: true`) — pola
lengkapnya sudah pernah didiskusikan sebelumnya di percakapan ini;
struktur `Scenario`/`registry.py` di boilerplate ini TIDAK PERLU
berubah sama sekali untuk pindah ke K8s, karena keduanya sudah
independen dari cara job dieksekusi (lihat `scenario.script_path` yang
sengaja generik, bisa diartikan sebagai path lokal maupun path di
dalam container image).
