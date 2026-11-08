# Sumber, asumsi, dan status skenario

Dokumen ini mencatat, untuk setiap skenario di folder ini, dari mana
definisinya berasal, rujukan yang mendukungnya, bagian yang merupakan
asumsi desain generator, dan celah yang masih terbuka terhadap Kajian
CATS v5 [CATS]. Rentang parameter lengkap ada di `params` tiap file
skenario. Perbarui dokumen ini setiap kali definisi Kajian, `params`, atau
`policy.json` berubah.

## Cara membaca

- **Ground truth berasal dari konstruksi data.** Label sesi adalah
  skenario yang dijalankan (`label`, `scenario_code`, `label_class`), dan
  parameter yang disuntikkan tercatat di kolom `param_*`. Rujukan di bawah
  tidak menciptakan ground truth; rujukan menunjukkan bahwa pola yang
  disimulasikan memang terjadi di cluster nyata, atau bahwa kebijakan yang
  dilanggar memang ada.
- **Rujukan tidak menetapkan angka parameter.** Ukuran memori, durasi,
  rasio, dan ambang di `params` maupun `policy.json` adalah asumsi desain
  generator, kecuali disebut lain.
- **Validitas dataset diperiksa dalam tiga lapis:**
  1. *Injeksi*: telemetri tiap sesi menunjukkan perilaku yang disuntikkan
     (lihat [Cek ground truth](#cek-ground-truth)).
  2. *Definisi*: skenario sesuai definisi Kajian (dokumen ini).
  3. *Realisme*: pola mirip trace cluster nyata (lihat
     [Validasi realisme](#validasi-realisme); belum dikerjakan).

Status terhadap Kajian:

| Status | Arti |
|---|---|
| Sesuai | pola simulasi sesuai definisi dan pola simulasi di Kajian |
| Sebagian | inti definisi terpenuhi, sebagian indikator Kajian belum dimodelkan |
| Berbeda | yang diimplementasikan berbeda dari definisi Kajian; perlu keputusan tim |
| Asumsi | tidak didefinisikan di Kajian; workload pembanding rancangan generator |

## Ringkasan

| Skenario | Kode | Kelas | Status | Rujukan |
|---|---|---|---|---|
| `misuse_idle_hoarding` | R1 | K2 | Sesuai | [JEON19], [WENG22], [CARVER25] |
| `misuse_idle_session` | R2 | K2 | Sesuai; ambang idle belum dari CloudX | [JH-CULL], [CARVER25] |
| `misuse_quota_exceed` | R3 | K2 | Berbeda | [K8S-RQ], [CLOUDX26], [NV-TS] |
| `misuse_overprovisioning` | R4 | K1 | Sebagian | [RZADCA20] |
| `misuse_zombie_job` | R5 | K2 | Sebagian | [KOOMEY15] |
| `misuse_concurrent_sessions` | R6 | K2 | Sesuai; batas sesi belum dari CloudX | [CLOUDX26], [JH-NAMED] |
| `misuse_inefficient_gpu` | R7 | K1 | Sesuai | [GAO24] |
| `misuse_gpu_monopolization` | P5 | K2 | Sebagian | [MAHAJAN20], [JEON19], [WENG22] |
| `misuse_synthetic_load` | S1 | K3 | Sebagian (proxy) | [MITRE], [KARN21], [TEKINER21] |
| `misuse_disguised_pattern` | S1 | K3 | Sebagian (proxy, perluasan) | [MITRE], [TEKINER21] |
| `benign_cnn_training` | BENIGN | - | Asumsi | [CLOUDX26] W1, [JEON19], [HU21] |
| `benign_data_pipeline_training` | BENIGN | - | Asumsi | [CLOUDX26] W1, [GAO24] |
| `benign_batch_inference` | BENIGN | - | Asumsi | [CLOUDX26] W2 |
| `benign_interactive_gpu_notebook` | BENIGN | - | Asumsi | [CARVER25] |
| `benign_eda_notebook` | BENIGN | - | Asumsi | [CLOUDX26] W3 |

Skenario prioritas Kajian (Bagian 5): R1, R2, R3, R6, P5, S1. R2, R5, dan
`benign_interactive_gpu_notebook` belum punya rujukan paper peer-review;
rujukannya berupa dokumentasi, preprint, atau laporan industri.

## Cek ground truth

`validate_dataset.py` (bagian 6) menguji tiap sesi terhadap definisi
skenarionya dan menulis hasilnya ke `ground_truth_check.csv`. Hanya sesi
berstatus *lolos* yang masuk `dataset_verified.csv`. Ambang kebijakan diambil
dari `policy.json` dan kolom `quota_mem_mb`; ambang verifikasi lain
didefinisikan di bagian atas cek ground truth di `validate_dataset.py`.

| Skenario | Sesi lolos bila |
|---|---|
| R1 | GPU tidak dipakai (p90 utilisasi ≤ 10%), memori tertahan ≥ 90% `param_hold_mem_mb`, VRAM ≤ kuota, antrean > 0, tidak melewati ambang idle |
| R2 | `last_activity_age_sec` melewati ambang idle |
| R3 | VRAM melewati kuota |
| R4 | request CPU ≥ 8x pemakaian rata-rata |
| R5 | tanpa aktivitas sepanjang sesi, GPU tidak dipakai, melewati ambang idle |
| R6 | sesi aktif pengguna melewati batas |
| R7 | median CPU ≥ 80% (≈1 core), rata-rata utilisasi GPU ≤ 60% |
| P5 | antrean di sepertiga akhir sesi lebih panjang dari sepertiga awal |
| S1 `synthetic_load` | median utilisasi ≥ 60%, tanpa aktivitas setelah awal sesi |
| S1 `disguised_pattern` | autokorelasi utilisasi pada periode yang disuntikkan ≥ 0.3, tanpa aktivitas setelah awal sesi |
| Benign | VRAM ≤ kuota, tidak melewati ambang idle, sesi aktif ≤ batas |

Status lain: *gagal* (ada cek yang tidak terpenuhi), *tidak lengkap* (data
untuk cek tidak ada, mis. Prometheus kosong atau sesi terlalu pendek), dan
*tanpa cek*. Cek yang bergantung GPU (kuota, antrean) tidak berlaku di mode
CPU. Sesi yang gagal dijalankan tidak masuk dataset sama sekali; alasannya
ada di `<session_id>.failed.json`.

## Kebijakan sandbox

Nilai di `run_experiment.py` (`POLICY`, `QUOTA_FRACS`,
`DEFAULT_REQUEST_PROFILES`) yang ditulis ke `policy.json` atau dipakai
semua sesi:

| Nilai | Di generator | Sumber | Catatan |
|---|---|---|---|
| `idle_timeout_sec` | 120 s | asumsi (kompresi waktu) | Kajian 6.4 meminta nilai dari konfigurasi idle culler CloudX. Default jupyterhub-idle-culler 600 s, dan JupyterHub memperbarui `last_activity` tiap 5 menit secara default [JH-CULL], jadi 120 s tidak dapat ditegakkan dengan setting default. Durasi minimum R2/R5 mengikuti nilai ini otomatis. |
| `max_concurrent_sessions` | 2 | asumsi | CloudX menerapkan kuota per-tenant berupa jumlah maksimum sesi bersamaan [CLOUDX26], tetapi nilainya tidak disebutkan. Mekanisme setara di JupyterHub: `named_server_limit_per_user` [JH-NAMED]. |
| `quota_mem_mb` | 50% atau 75% VRAM node | asumsi | Kuota memori GPU per sesi, dipakai R3. Lihat catatan R3. |
| profil `requests` | 1 CPU/1 GiB atau 2 CPU/2 GiB | asumsi | Semua sesi diberi request agar ada/tidaknya request bukan penanda label; R4 memakai profil sendiri. |

## Rincian skenario misuse

### R1 — `misuse_idle_hoarding` (K2)

- **Definisi Kajian:** menguasai alokasi GPU lama dengan pemanfaatan
  rendah ketika pengguna lain menunggu. Pola simulasi: alokasi GPU tinggi,
  utilisasi rendah berkepanjangan, antrean > 0. Rujukan Kajian: [K8S-RQ],
  [JH-CULL].
- **Implementasi:** menahan VRAM tanpa komputasi, membuka notebook berkala
  (keep-alive), 1–3 Pod waiter mengantre GPU.
- **Rujukan pendukung:** [JEON19] dan [WENG22] melaporkan utilisasi GPU
  rendah dan antrean panjang di cluster GPU multi-tenant produksi.
  [CARVER25]: platform notebook memegang GPU sepanjang sesi walaupun
  pemakaiannya sporadis.
- **Asumsi:** VRAM ditahan 50–85% dari kuota sesi itu sendiri, sehingga
  tidak pernah melewati kuota (R1 terpisah dari R3); keep-alive 30–110 s
  (di bawah ambang idle, agar R1 terpisah dari R2).

### R2 — `misuse_idle_session` (K2)

- **Definisi Kajian:** sesi JupyterHub menguasai resource tanpa aktivitas
  bermakna melebihi kebijakan idle. Indikator: last activity, kernel
  activity, session age, alokasi GPU. Rujukan Kajian: [JH-CULL], [K8S-RQ].
- **Implementasi:** aktif di 10–35% awal sesi, lalu kernel idle sementara
  GPU tetap teralokasi. Durasi minimum menjamin fase idle ≥ ambang + 30 s,
  termasuk saat `--duration-scale` atau `--duration-sec` memperpendek sesi.
- **Rujukan pendukung:** [JH-CULL] untuk mekanisme `last_activity` dan
  idle timeout; [CARVER25].
- **Asumsi:** ambang idle 120 s dan porsi aktif 10–35%.
- **Celah:** ambang belum dari CloudX (Kajian 6.4).

### R3 — `misuse_quota_exceed` (K2)

- **Definisi Kajian:** penggunaan GPU-hours atau jumlah GPU melampaui
  kuota. Pola simulasi: akumulasi GPU-hours melampaui kuota. Rujukan
  Kajian: [K8S-RQ].
- **Implementasi:** VRAM 1.3–1.8x kuota memori sesi (`quota_mem_mb`).
- **Mengapa berbeda:** [K8S-RQ] hanya mengatur kuota jumlah extended
  resource seperti `requests.nvidia.com/gpu`, bukan VRAM atau GPU-hours.
- **Argumen bila VRAM dipertahankan:** CloudX menerapkan kuota per-tenant
  berupa "GPU shares/MIG slices" dan menjalankan beberapa sesi per GPU
  dengan time-slicing [CLOUDX26]. Time-slicing tidak memberi isolasi
  memori antar-Pod yang berbagi GPU [NV-TS], sehingga sesi yang memakai
  VRAM melebihi porsinya mengganggu sesi lain di GPU yang sama.
- **Keputusan tim:** (a) ikuti Kajian dengan kuota GPU-hours per pengguna
  lintas sesi, atau (b) pertahankan VRAM sebagai varian R3 lalu tambahkan
  ke definisi di Kajian.
- **Celah implementasi:** alokasi dibatasi 0.9x VRAM, sehingga sesi dengan
  kuota 75% selalu 1.20x kuota (bukan 1.3–1.8x); menunggu keputusan
  definisi R3. Bila alokasi gagal, job error dan sesinya tidak disimpan.

### R4 — `misuse_overprovisioning` (K1)

- **Definisi Kajian:** permintaan GPU/CPU/RAM jauh di atas kebutuhan aktual
  secara berulang. Indikator: requested vs used, tren beberapa sesi.
  Rujukan Kajian: [K8S-RQ].
- **Implementasi:** request 4–8 CPU dan 3–4 GiB, pemakaian di bawah 0.5
  core.
- **Rujukan pendukung:** [RZADCA20]: limit yang diatur manual cenderung
  berlebih; slack (selisih limit dan pemakaian) 46% pada job yang dikelola
  manual.
- **Asumsi:** profil request dan beban ringan.
- **Celah:** unsur "berulang" belum dimodelkan; tiap sesi R4 berdiri
  sendiri dan `user_id` diundi acak. Request GPU tidak divariasikan.

### R5 — `misuse_zombie_job` (K2)

- **Definisi Kajian:** pekerjaan atau sesi tetap berjalan setelah
  ditinggalkan. Indikator: aktivitas pengguna, progres job, lifetime,
  output terbaru. Kelas K1, atau K2 bila melampaui kebijakan
  idle/lifetime. Rujukan Kajian: [JH-CULL].
- **Implementasi:** menahan 5–20% VRAM, tanpa aktivitas sejak awal, kelas
  selalu K2. Durasi minimum = ambang idle + 30 s, sehingga sesi selalu
  melewati ambang idle.
- **Rujukan pendukung:** [KOOMEY15] (laporan industri): sekitar 30% server
  dalam sampel tidak memberi layanan berguna selama 6 bulan atau lebih.
  Belum ada rujukan peer-review khusus GPU atau notebook.
- **Celah:** `policy.json` belum punya batas lifetime, jadi kelas K2 hanya
  bersandar pada ambang idle. Skenario tidak punya fase kerja berguna
  sebelum ditinggalkan.

### R6 — `misuse_concurrent_sessions` (K2)

- **Definisi Kajian:** membuka sesi simultan melebihi batas yang
  diizinkan. Indikator: jumlah sesi aktif dan total resource per pengguna.
  Kajian 6.2 menyebut R6 cukup dideteksi berbasis aturan. Rujukan Kajian:
  [K8S-RQ], [JH-CULL].
- **Implementasi:** sesi utama wajar ditambah 2–4 sesi sibling milik
  pengguna yang sama (Pod ringan tanpa GPU), total 3–5 sesi.
- **Rujukan pendukung:** [CLOUDX26]: kuota per-tenant berupa jumlah
  maksimum sesi bersamaan, ditegakkan lewat admission control saat sesi
  dibuat. [JH-NAMED]: `named_server_limit_per_user`.
- **Celah:** batas 2 sesi masih asumsi.

### R7 — `misuse_inefficient_gpu` (K1)

- **Definisi Kajian:** workload tidak efisien karena bottleneck atau
  konfigurasi, bukan otomatis misuse. Indikator: utilisasi GPU/CPU, I/O,
  throughput, memori GPU. Rujukan Kajian: [K8S-RQ].
- **Implementasi:** preprocessing per sampel di loop Python satu thread:
  CPU sekitar 1 core penuh, GPU rendah, progres training tetap ada.
- **Rujukan pendukung:** [GAO24]: sekitar 46% isu utilisasi GPU rendah
  pada 400 job nyata berkaitan dengan operasi data.
- **Asumsi:** ukuran batch dan gambar.

### P5 — `misuse_gpu_monopolization` (K2)

- **Definisi Kajian:** dominasi alokasi GPU yang mengganggu akses pengguna
  lain; perlu definisi fairness lokal. Pola simulasi: porsi alokasi
  dominan disertai kenaikan waktu tunggu pengguna lain. Rujukan Kajian:
  [K8S-RQ].
- **Implementasi:** training intensif 600–1200 s, 2–4 waiter datang
  bertahap sehingga antrean dan waktu tunggu terus naik.
- **Rujukan pendukung:** [MAHAJAN20]: finish-time fairness sebagai metrik
  fairness di cluster GPU. [JEON19], [WENG22]: antrean dan waktu tunggu
  panjang di cluster GPU produksi.
- **Celah:** metrik fairness lokal masih keputusan terbuka (Kajian 6.4),
  dan data belum punya kolom porsi alokasi per pengguna.

### S1 — `misuse_synthetic_load` (K3)

- **Definisi Kajian:** memakai resource komputasi untuk mining tanpa izin.
  Indikator: proses mencurigakan, beban komputasi persisten, koneksi mining
  pool. Pola simulasi: beban tinggi dan konstan, aktivitas notebook rendah.
  Rujukan Kajian: [MITRE].
- **Implementasi:** perkalian matriks terus-menerus (duty 70–100%),
  aktivitas notebook hanya di awal. Ini proxy beban, bukan software mining.
- **Rujukan pendukung:** [KARN21]: deteksi cryptominer di Pod Kubernetes
  lewat system call. [TEKINER21]: sistematisasi malware cryptojacking.
- **Celah:** proses mencurigakan dan koneksi ke mining pool tidak
  disimulasikan (kolom jaringan sekitar 0); tulis sebagai batasan.

### S1 — `misuse_disguised_pattern` (K3)

- **Definisi Kajian:** sama dengan S1. Varian berosilasi tidak dijelaskan
  di Kajian, jadi ini perluasan.
- **Implementasi:** intensitas GPU (porsi waktu sibuk per siklus 0,5 s)
  mengikuti gelombang sinus antara `min_intensity` (0,1–0,4) dan
  `max_intensity` (0,7–1,0) dengan periode 30–120 s, tanpa aktivitas
  notebook. `min_duration_sec` = 2 × periode + 30 s, sehingga cek
  periodisitas selalu punya dua periode, juga dengan `--duration-scale 0.5`
  atau `--duration-sec` uji cepat.
- **Rujukan pendukung:** [TEKINER21] membahas teknik evasi dan obfuskasi
  cryptojacking.
- **Celah:** v1/v2 menghitung beban sebagai jumlah perkalian matriks per
  siklus, sehingga besarnya bergantung kecepatan GPU: osilasi nyaris tidak
  terlihat di dataset v1, dan di RTX 5060 Ti (PC lab) hanya ~1–17% GPU (cek
  `pola_periodik` gagal pada sesi 300 s, ACF −0,34). Versi berbasis waktu
  diverifikasi di PC lab: sinus 35↔90% dengan periode 92 s, ACF 0,99. Sinus
  juga tampak di `cpu_pct`, karena `torch.cuda.synchronize()` menunggu
  dengan spin-wait (miner sungguhan memakai sedikit CPU host).

## Rincian skenario benign

Skenario benign tidak didefinisikan di Kajian. Workload acuannya diambil
dari evaluasi CloudX [CLOUDX26, Tabel 3]:

| ID | Workload CloudX | VRAM | Durasi |
|---|---|---|---|
| W1 | training ResNet-50 v1.5 / CIFAR-10, batch 128 | ~6.2 GB | ~4.8 menit |
| W2 | inference BERT-base, batch 64; 3 sesi per GPU (time-slicing) | ~3.1 GB | ~2.1 menit |
| W3 | preprocessing Pandas/NumPy (CSV 4 GB) di node CPU-only | < 0.5 GB | ~3.5 menit |

Memori benign di generator berukuran tetap dan jauh lebih kecil daripada
W1–W2 (audit v1: training sekitar 0.3 GB). Akibatnya, di GPU berkapasitas
besar, memori GPU tinggi hampir selalu berarti misuse dalam data ini.

### `benign_cnn_training`

- **Pola:** training CNN berulang per epoch, checkpoint opsional, aktivitas
  notebook tiap epoch.
- **Rujukan:** [CLOUDX26] W1; [JEON19] dan [HU21] untuk karakteristik job
  training di cluster GPU.
- **Catatan:** siklus per epoch (sekitar 1–2 s) lebih pendek dari sampling
  5 s, jadi utilisasi GPU terlihat datar (audit v1: ~98%). Deskripsi di
  kode sudah disesuaikan.

### `benign_data_pipeline_training`

- **Pola:** membaca shard dari disk, augmentasi di CPU, training berbasis
  waktu per shard (`train_sec_per_shard` 5–20 s), evaluasi, checkpoint
  opsional; GPU turun sesaat ketika shard dimuat dan I/O baca muncul per
  shard. Dulu tiap shard hanya dilatih satu kali lewat (puluhan step kecil),
  sehingga di GPU cepat job menjadi CPU-bound: di PC lab GPU 32–41% dengan
  CPU 1 core penuh dan baca disk ~400 MB/s terus-menerus, yang memenuhi
  definisi R7. Setelah perbaikan (PC lab): GPU 91–93%, baca ~1,3 MB/s. Dip
  saat loading (~0,1 s per shard) terlalu singkat untuk terlihat di sampel
  5 s.
- **Rujukan:** [CLOUDX26] W1; [GAO24]: operasi data adalah penyebab umum
  utilisasi GPU rendah. Di sini menjadi pembanding sah untuk R1 dan R7.

### `benign_batch_inference`

- **Pola:** model dimuat sekali, batch datang tidak teratur, prediksi
  opsional ditulis ke disk.
- **Rujukan:** [CLOUDX26] W2.
- **Celah:** pada audit v1, VRAM mencapai ~3.9 GB (hampir penuh di GPU
  4 GB) karena batch sampai 512, sehingga 10/10 sesi gagal cek
  `di_bawah_kuota`. Kode v2 membatasi batch sampai 256, belum diverifikasi.

### `benign_interactive_gpu_notebook`

- **Pola:** burst GPU/CPU per cell diselingi jeda berpikir; aktivitas
  notebook rutin. Pembanding sulit untuk R1/R2.
- **Rujukan:** [CARVER25]: pemakaian GPU di sesi notebook bersifat
  intermiten.
- **Asumsi:** jeda berpikir maksimum 20–100 s, di bawah ambang idle.

### `benign_eda_notebook`

- **Pola:** olah data pandas/numpy tanpa GPU.
- **Rujukan:** [CLOUDX26] W3.
- **Celah:** di mode GPU, generator tetap meminta GPU untuk sesi ini (semua
  sesi meminta GPU), sedangkan W3 di CloudX berjalan di node CPU-only.
  CPU ringan-sedang (audit v1: ~7%); deskripsi di kode sudah disesuaikan.

## Validasi realisme

Kajian Bagian 6.0 menyebut Google Cluster Trace dan Alibaba GPU Cluster
Trace sebagai pembanding validitas TA 1; rujukan standar keduanya adalah
[REISS12] dan [WENG22]. Trace Alibaba (`cluster-trace-gpu-v2020`) memuat
utilisasi GPU dan memori per instance, sehingga distribusinya bisa
dibandingkan dengan data generator. Perbandingan ini belum dikerjakan.

`gpu_util_pct` dan `gpu_mem_util_pct` mengikuti definisi nvidia-smi
[NV-SMI]: persen waktu ketika kernel berjalan, dan persen waktu ketika
memori dibaca atau ditulis. Keduanya bukan okupansi VRAM; okupansi ada di
`gpu_mem_used_mb_device`.

"Audit v1" di dokumen ini merujuk ke dataset `data-k0s-20261025`, yang
dihasilkan kode commit `4aed548` di RTX 3050 Laptop 4 GB.

## Daftar rujukan

Jenis: **paper** (peer-review), **preprint**, **dok** (dokumentasi
teknis), **laporan** (laporan industri), **internal** (dokumen tim).

- **[CATS]** (internal) Kajian Misuse Detection pada CloudX Analytics
  Trace Sandbox (CATS), dokumen kerja versi 5, 23 September 2026.
- **[K8S-RQ]** (dok; Kajian [A]) Kubernetes. *Resource Quotas*.
  <https://kubernetes.io/docs/concepts/policy/resource-quotas/>
- **[JH-CULL]** (dok; Kajian [B]) JupyterHub. *jupyterhub-idle-culler*.
  <https://github.com/jupyterhub/jupyterhub-idle-culler>
- **[MITRE]** (dok; Kajian [F]) MITRE ATT&CK. *T1496.001: Compute
  Hijacking*. <https://attack.mitre.org/techniques/T1496/001/>
- **[JH-NAMED]** (dok) JupyterHub. *Configuring user environments* (named
  servers, `named_server_limit_per_user`).
  <https://jupyterhub.readthedocs.io/en/stable/howto/configuration/config-user-env.html>
- **[NV-TS]** (dok) NVIDIA GPU Operator. *Time-Slicing GPUs in
  Kubernetes*.
  <https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-sharing.html>
- **[NV-SMI]** (dok) NVIDIA. Definisi field query nvidia-smi
  (`nvidia-smi --help-query-gpu`).
- **[CLOUDX26]** (paper) Shiddiqi, A. M., Ijtihadie, R. M., Kale, T. G.,
  Akbar, R. J., Navastara, D. A., Fahriani, N., Pepuho, G. C. D.,
  Aptanagi, U., & De La Croix, N. J. (2026). CloudX: A dual-layered cloud
  native architecture for GPU resource management using interactive
  notebook. *Array*, 31, 101026.
  <https://doi.org/10.1016/j.array.2026.101026>
- **[CARVER25]** (preprint) Carver, B., Zhang, J., Wang, H., Mahadik, K.,
  & Cheng, Y. (2025). NotebookOS: A Replicated Notebook Platform for
  Interactive Training with On-Demand GPUs. arXiv:2503.20591.
- **[JEON19]** (paper) Jeon, M., Venkataraman, S., Phanishayee, A., Qian,
  J., Xiao, W., & Yang, F. (2019). Analysis of Large-Scale Multi-Tenant GPU
  Clusters for DNN Training Workloads. *USENIX ATC '19*, 947–960.
- **[WENG22]** (paper) Weng, Q., et al. (2022). MLaaS in the Wild:
  Workload Analysis and Scheduling in Large-Scale Heterogeneous GPU
  Clusters. *NSDI '22*. Trace: <https://github.com/alibaba/clusterdata>
  (`cluster-trace-gpu-v2020`).
- **[HU21]** (paper) Hu, Q., Sun, P., Yan, S., Wen, Y., & Zhang, T.
  (2021). Characterization and Prediction of Deep Learning Workloads in
  Large-Scale GPU Datacenters. *SC '21*.
- **[GAO24]** (paper) Gao, Y., et al. (2024). An Empirical Study on Low
  GPU Utilization of Deep Learning Jobs. *ICSE '24*.
  <https://doi.org/10.1145/3597503.3639232>
- **[RZADCA20]** (paper) Rzadca, K., et al. (2020). Autopilot: Workload
  Autoscaling at Google Scale. *EuroSys '20*.
- **[MAHAJAN20]** (paper) Mahajan, K., et al. (2020). Themis: Fair and
  Efficient GPU Cluster Scheduling. *NSDI '20*, 289–304.
- **[KARN21]** (paper) Karn, R. R., Kudva, P., Huang, H., Suneja, S., &
  Elfadel, I. M. (2021). Cryptomining Detection in Container Clouds Using
  System Calls and Explainable Machine Learning. *IEEE TPDS*, 32(3),
  674–691. <https://doi.org/10.1109/TPDS.2020.3029088>
- **[TEKINER21]** (paper) Tekiner, E., Acar, A., Uluagac, A. S., Kirda,
  E., & Selcuk, A. A. (2021). SoK: Cryptojacking Malware. *IEEE EuroS&P
  2021*. <https://doi.org/10.1109/EuroSP51992.2021.00019>
- **[KOOMEY15]** (laporan) Koomey, J., & Taylor, J. (2015). Studi server
  "comatose" (Anthesis Group, data TSO Logic).
  <https://www.anthesisgroup.com/report-finds-that-thirty-per-cent-of-servers-are-comatose/>
- **[REISS12]** (paper) Reiss, C., Tumanov, A., Ganger, G. R., Katz, R.
  H., & Kozuch, M. A. (2012). Heterogeneity and Dynamicity of Clouds at
  Scale: Google Trace Analysis. *SoCC '12*.
  <https://doi.org/10.1145/2391229.2391236>
