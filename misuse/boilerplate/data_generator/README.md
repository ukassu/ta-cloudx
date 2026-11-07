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
| benign_cnn_training | BENIGN | - | GPU tinggi per epoch, checkpoint opsional | antrean 0-2 |
| benign_eda_notebook | BENIGN | - | CPU pandas, GPU ~0 | 0-1 sesi paralel (di bawah batas) |
| benign_interactive_gpu_notebook | BENIGN | - | burst per cell + jeda berpikir | antrean 0-1, 0-1 sesi paralel |
| benign_data_pipeline_training | BENIGN | - | baca shard dari disk, dip GPU saat data loading | antrean 0-2 |
| benign_batch_inference | BENIGN | - | burst GPU per batch, tulis prediksi opsional | antrean 0-1 |
| misuse_idle_hoarding | R1 | K2 | VRAM 50-85% kuota sesi ditahan, util ~0, keep-alive | antrean 1-3 |
| misuse_idle_session | R2 | K2 | aktif 10-35% awal sesi lalu kernel idle melewati ambang idle | antrean 0-1 |
| misuse_quota_exceed | R3 | K2 | VRAM 1.3-1.8x `quota_mem_mb` sesi | antrean 0-1 |
| misuse_overprovisioning | R4 | K1 | request 4-8 CPU / 3-4 GiB, pakai < 0.5 core | - |
| misuse_zombie_job | R5 | K2 | flat, tanpa aktivitas sejak awal | - |
| misuse_concurrent_sessions | R6 | K2 | sesi wajar + 2-4 sesi paralel (> batas 2) | sibling |
| misuse_inefficient_gpu | R7 | K1 | preprocessing Python 1 thread, GPU rendah, CPU penuh | antrean 0-2 |
| misuse_gpu_monopolization | P5 | K2 | training intensif 600-1200 s | antrean 2-4 bertahap |
| misuse_synthetic_load | S1 | K3 | GPU 70-100% flat, tanpa aktivitas | - |
| misuse_disguised_pattern | S1 | K3 | osilasi GPU teratur, tanpa aktivitas | - |

Rentang parameter lengkap ada di `params` tiap file skenario. Sumber
definisi, rujukan, asumsi, dan celah tiap skenario terhadap Kajian ada di
[`scenarios/README.md`](scenarios/README.md).

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
| `gpu_mem_total_mb` | sidecar | total VRAM node; konteks untuk normalisasi lintas mesin, bukan fitur |
| `prom_queue_length` | Prometheus | Pod waiter GPU milik sesi ini yang Pending (mode GPU) |
| `prom_queue_wait_max_sec` | Prometheus | waktu tunggu waiter terlama |
| `prom_user_active_sessions` | Prometheus | sesi utama + sibling yang Running |
| `quota_mem_mb` | run_experiment | kuota memori GPU sesi (50%/75% memori GPU node), semua sesi |
| `param_*` | run_experiment | parameter yang diundi untuk sesi ini -- ground truth, **bukan fitur** |

Ambang kebijakan sandbox (idle, batas sesi, memori GPU node) ditulis ke
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
# Rencana 300 sesi (85% benign, 15% misuse), parameter diundi per sesi
python run_experiment.py --executor k8s --sessions 300 --out-dir data-v2

# Terputus? Lanjutkan rencana yang sama; sesi selesai dilewati
python run_experiment.py --executor k8s --resume --out-dir data-v2

# Uji cepat: 1 sesi per skenario, durasi 60 s (P5 3x; R2/R5 lebih lama
# agar melewati ambang idle)
python run_experiment.py --executor k8s --sessions 15 --misuse-frac 0.67 \
    --duration-sec 60 --out-dir data-test

# Laporan validitas dataset (variasi, kebocoran label, konsistensi
# kebijakan, cek ground truth per sesi)
python validate_dataset.py data-v2

# Uji 1 skenario secara standalone (tanpa runner/telemetry)
python scenarios/misuse_idle_hoarding.py --duration-sec 30
```

Opsi rencana: `--sessions`, `--misuse-frac`, `--seed` (rencana identik
untuk seed sama), `--duration-scale` (pengali durasi yang diundi),
`--scenarios a,b` (subset). Rencana disimpan di `<out-dir>/plan.json`;
out-dir yang sudah berisi rencana hanya bisa dilanjutkan dengan `--resume`.
`plan.json` juga mencatat nama GPU, total VRAM, dan pemakaian GPU saat run
dimulai (`gpu`; peringatan bila GPU sudah terpakai, mis. oleh layar).
Parameter MB di rencana diundi dari VRAM mesin pembuatnya, jadi `--resume`
ditolak di mesin dengan memori GPU berbeda -- buat rencana baru per mesin.

### Variasi per sesi (v2)

Setiap skenario punya `params` (rentang parameter) yang diundi per
sesi dengan RNG dari `--seed`, dioper sebagai argumen CLI, dan dicatat
sebagai kolom `param_*`. Parameter memori ditulis sebagai porsi memori
GPU node (`mem_frac`), jadi rencana menyesuaikan GPU 4 GB maupun 16 GB.
Untuk mencegah kebocoran label struktural:

- semua sesi punya `requests` (profil default 1 CPU/1 GiB atau 2 CPU/2 GiB;
  R4 jauh lebih besar), sehingga kolom request tidak pernah kosong;
- semua sesi punya `quota_mem_mb`; R3 adalah sesi yang melampauinya;
- durasi diundi dan tumpang-tindih antar skenario;
- semua skenario bisa menulis log berkala (`COMMON_PARAMS`), dan
  checkpoint/penyimpanan hasil benign bersifat opsional;
- skenario yang labelnya mensyaratkan ambang kebijakan terlewati (R2, R5)
  punya durasi minimum (`Scenario.min_duration_sec`), sehingga ambang idle
  tetap terlewati walau `--duration-scale`/`--duration-sec` memperpendek
  durasi.

Output: `<out-dir>/<session_id>.csv` per sesi, digabung otomatis jadi
`<out-dir>/dataset.csv` di akhir proses. Sesi yang gagal (exit code ≠ 0,
timeout, atau telemetri tidak terambil) tidak masuk dataset karena
perilakunya belum tentu sesuai label; alasan dan ekor log job dicatat di
`<out-dir>/<session_id>.failed.json`, dan `--resume` mengulangnya.

`validate_dataset.py` menguji tiap sesi terhadap definisi skenarionya (cek
ground truth; daftar cek ada di [`scenarios/README.md`](scenarios/README.md))
dan menulis `ground_truth_check.csv` serta `dataset_verified.csv`, yang
hanya berisi sesi yang lolos. Pakai `dataset_verified.csv` untuk melatih
dan mengevaluasi model.

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
| `PROM_REQUIRED` | `false` | `true` = sesi yang tetap tanpa metrik Prometheus setelah 3 percobaan dicatat gagal (`.failed.json`) dan diulang `--resume` |
| `K8S_REQUEST_GPU` | `false` | `true` = container job meminta `nvidia.com/gpu: 1`, sidecar membaca `nvidia-smi` |
| `JOB_IMAGE` | `local/data-generator-job:latest` | pakai `local/data-generator-job:gpu` (torch CUDA) saat mode GPU |
| `CODE_SOURCE` | `configmap` | kode skenario/sidecar dikirim lewat ConfigMap `data-generator-code` dan di-mount ke `/app`; `image` = pakai kode yang dibake di image |

Dengan `CODE_SOURCE=configmap` image cukup berisi dependency: mengubah
`scenarios/*.py` atau `telemetry_sidecar.py` tidak perlu rebuild image,
dan hash kode dicatat di `plan.json` (`code_sha256`).

Image GPU dibangun dari Dockerfile yang sama:

```bash
docker build -f docker/Dockerfile.job \
  --build-arg TORCH_INDEX_URL=https://download.pytorch.org/whl/cu128 \
  -t local/data-generator-job:gpu .
```

File `.merged.csv` memiliki kolom lokal (`cpu_pct`, `mem_rss_mb`, dan
sebagainya), ditambah kolom Prometheus dengan prefix `prom_`. Metrik
CPU, memory, I/O, restart, dan request/limit hanya dari container
`job` (sidecar tidak ikut terhitung); metrik network bersifat per Pod.
Jika suatu metrik tidak tersedia, nilainya kosong. Query Prometheus
diulang hingga 3 kali bila gagal (DNS/ingress) atau belum ada data
container `job`. Bila tetap gagal, data `psutil` tetap tersimpan dan
kolom `prom_*` kosong; dengan `PROM_REQUIRED=true` sesi itu dicatat gagal
dan diulang `--resume`, sehingga dataset tidak berisi sesi tanpa metrik
Prometheus (missingness seperti itu bisa berkorelasi dengan label).

Query runner hanya membaca series yang di-scrape langsung oleh
kube-prometheus-stack (filter `pipeline!="otel"`, `PROM_SERIES_FILTER` di
`k8s_job_runner.py`). `supporting/9-install-otel-collector.sh` mengekspor
ulang cAdvisor dan kube-state-metrics ke Prometheus yang sama dengan label
`pipeline="otel"`; tanpa filter, `sum()`/`count()` menjumlahkan dua salinan
sehingga hampir semua kolom `prom_*` bernilai 2x.

Perbedaan semantik kolom lokal vs Prometheus:

| Lokal (psutil) | Prometheus | Catatan |
|----------------|------------|---------|
| `cpu_pct` (100 = 1 core, sesaat) | `prom_cpu_pct` (100 = 1 core, rata-rata `PROM_RATE_WINDOW`) | Prometheus lebih halus/tertinggal |
| `mem_rss_mb` (RSS proses) | `prom_mem_rss_mb` (working set container) | nilai mirip |
| `io_read_mb`, `io_write_mb` (**kumulatif** MB) | `prom_io_read_mb`, `prom_io_write_mb` (**laju** MB/s) | jangan dicampur |

Kernel tanpa `/proc/<pid>/io` (`CONFIG_TASK_IO_ACCOUNTING` mati, misalnya
kernel WSL2 6.18 di PC lab) membuat psutil tidak punya `io_counters()`. Di
kernel seperti itu sidecar mengisi `io_read_mb`/`io_write_mb` dari `io.stat`
cgroup container `job` (byte kumulatif yang sampai ke storage, sumber yang sama
dengan cAdvisor), dan log sidecar menyebut `sumber io: cgroup io.stat`.

## Metadata tambahan per skenario

Metadata statis yang perlu ikut menjadi kolom CSV ditaruh di
`Scenario.extra_meta`; nilai yang bervariasi per sesi ditaruh di
`Scenario.params` (otomatis menjadi kolom `param_*`). Kuota sesi
(`quota_mem_mb`) dibagikan run_experiment.py ke semua sesi; skenario
yang membutuhkannya sebagai argumen memakai `session_quota()`.

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
