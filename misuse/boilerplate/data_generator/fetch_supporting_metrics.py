"""
data_generator/fetch_supporting_metrics.py
============================================
Ambil metrik dari tools supporting (setup-cluster/supporting) untuk setiap
sesi sebuah run. dataset.csv hanya berisi telemetri sidecar dan kolom prom_*
dari cAdvisor + kube-state-metrics; metrik OpenTelemetry Collector
(kubeletstats, hostmetrics), Inspektor Gadget, dan NetObserv hanya tersimpan
di Prometheus. Skrip ini mengekspornya TANPA mengubah dataset.csv:

  <out_dir>/supporting_metrics.csv          1 baris per sesi per titik grid
                                            (grid = <session_id>.prometheus.csv)
  <out_dir>/dataset_extended.csv            dataset.csv + kolom supporting
                                            (sampel terdekat, toleransi = interval)
  <out_dir>/supporting_netobserv_flows.csv  flow NetObserv per pasangan namespace
                                            selama run

Jalankan selagi data masih ada di Prometheus (retensi 10 hari, dan TSDB-nya
emptyDir), atau terhadap arsip TSDB:

  ./prometheus_archive.sh serve data-v3-20261107-prometheus-tsdb
  PROMETHEUS_URL=http://127.0.0.1:19090 python fetch_supporting_metrics.py data-v3-20261107

Catatan sumber:
  - Kolom otel_*: receiver kubeletstats (interval 15 s), diekspor collector ke
    Prometheus dengan label pipeline="otel". Kolom *_page_faults, *_net_*_kb
    bersifat KUMULATIF sejak container/Pod dibuat.
  - Kolom host_* (hostmetrics) dan ig_* (Inspektor Gadget, gadget
    profile_blockio) berlevel HOST, bukan per Pod: semua node k0s di satu mesin
    berbagi kernel, jadi nilainya mencakup seluruh cluster.
  - NetObserv hanya menghasilkan series untuk Pod yang punya flow IP; skenario
    saat ini tidak memakai jaringan, sehingga tidak ada kolom per sesi.
  - Receiver k8s_cluster (k8s_pod_phase, k8s_container_*) tidak diambil: isinya
    sama dengan kube-state-metrics, dan exporter OTel mempertahankan series
    lama selama 5 menit (metric_expiration) sehingga nilainya bisa basi.
"""

import csv
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pandas as pd

import k8s_job_runner as runner

RATE_WINDOW = runner.PROM_RATE_WINDOW
MB = "/ 1024 / 1024"

# %(pod)s / %(job)s / %(sidecar)s = selector Pod sesi / container job / container
# telemetry pada series kubeletstats; %(node)s = node tempat Pod berjalan.
# max() hanya meratakan label: tiap metrik punya satu series per Pod/container.
# Dua collector agent (satu per node) melaporkan host yang sama -> max by collector.
SUPPORTING_QUERIES = {
    # --- OpenTelemetry kubeletstats: level Pod ---
    "otel_pod_cpu_cores": "max(k8s_pod_cpu_usage{%(pod)s})",
    "otel_pod_mem_ws_mb": f"max(k8s_pod_memory_working_set_bytes{{%(pod)s}}) {MB}",
    "otel_pod_mem_rss_mb": f"max(k8s_pod_memory_rss_bytes{{%(pod)s}}) {MB}",
    "otel_pod_page_faults": "max(k8s_pod_memory_page_faults_ratio{%(pod)s})",
    "otel_pod_major_page_faults": "max(k8s_pod_memory_major_page_faults_ratio{%(pod)s})",
    "otel_pod_net_rx_kb": 'sum(k8s_pod_network_io_bytes_total{%(pod)s,direction="receive"}) / 1024',
    "otel_pod_net_tx_kb": 'sum(k8s_pod_network_io_bytes_total{%(pod)s,direction="transmit"}) / 1024',
    "otel_pod_fs_usage_mb": f"max(k8s_pod_filesystem_usage_bytes{{%(pod)s}}) {MB}",
    # --- OpenTelemetry kubeletstats: container job ---
    "otel_job_cpu_cores": "max(container_cpu_usage{%(job)s})",
    "otel_job_mem_ws_mb": f"max(container_memory_working_set_bytes{{%(job)s}}) {MB}",
    "otel_job_mem_rss_mb": f"max(container_memory_rss_bytes{{%(job)s}}) {MB}",
    "otel_job_page_faults": "max(container_memory_page_faults_ratio{%(job)s})",
    "otel_job_major_page_faults": "max(container_memory_major_page_faults_ratio{%(job)s})",
    "otel_job_fs_usage_mb": f"max(container_filesystem_usage_bytes{{%(job)s}}) {MB}",
    # --- OpenTelemetry kubeletstats: container telemetry (overhead sidecar) ---
    "otel_sidecar_cpu_cores": "max(container_cpu_usage{%(sidecar)s})",
    "otel_sidecar_mem_ws_mb": f"max(container_memory_working_set_bytes{{%(sidecar)s}}) {MB}",
    # --- OpenTelemetry hostmetrics: level host ---
    "host_cpu_busy_cores": (
        'max(sum by (collector_pod) (rate(system_cpu_time_seconds_total{pipeline="otel",'
        'state!~"idle|wait"}[%(w)s])))'),
    "host_cpu_iowait_cores": (
        'max(sum by (collector_pod) (rate(system_cpu_time_seconds_total{pipeline="otel",'
        'state="wait"}[%(w)s])))'),
    # rincian host_cpu_busy_cores; softirq kernel tidak tercatat di cgroup Pod mana pun
    "host_cpu_user_cores": (
        'max(sum by (collector_pod) (rate(system_cpu_time_seconds_total{pipeline="otel",'
        'state="user"}[%(w)s])))'),
    "host_cpu_system_cores": (
        'max(sum by (collector_pod) (rate(system_cpu_time_seconds_total{pipeline="otel",'
        'state="system"}[%(w)s])))'),
    "host_cpu_softirq_cores": (
        'max(sum by (collector_pod) (rate(system_cpu_time_seconds_total{pipeline="otel",'
        'state="softirq"}[%(w)s])))'),
    "host_load1": 'max(system_cpu_load_average_1m{pipeline="otel"})',
    "host_mem_used_mb": f'max(system_memory_usage_bytes{{pipeline="otel",state="used"}}) {MB}',
    "host_mem_cached_mb": f'max(system_memory_usage_bytes{{pipeline="otel",state="cached"}}) {MB}',
    "host_disk_read_mbps": (
        'max(sum by (collector_pod) (rate(system_disk_io_bytes_total{pipeline="otel",'
        f'direction="read"}}[%(w)s]))) {MB}'),
    "host_disk_write_mbps": (
        'max(sum by (collector_pod) (rate(system_disk_io_bytes_total{pipeline="otel",'
        f'direction="write"}}[%(w)s]))) {MB}'),
    # --- Inspektor Gadget profile_blockio: histogram latensi block I/O (mikrodetik) ---
    "ig_blockio_ops_per_s": 'sum(rate(latency_s_count{pipeline="otel",node="%(node)s"}[%(w)s]))',
    "ig_blockio_lat_p50_us": (
        'histogram_quantile(0.5, sum by (le) (rate(latency_s_bucket{pipeline="otel",'
        'node="%(node)s"}[%(w)s])))'),
    "ig_blockio_lat_p99_us": (
        'histogram_quantile(0.99, sum by (le) (rate(latency_s_bucket{pipeline="otel",'
        'node="%(node)s"}[%(w)s])))'),
}
SUPPORTING_COLUMNS = list(SUPPORTING_QUERIES)


def _query_instant(query, at):
    """Query instan; return list (label, nilai) untuk SEMUA series hasil."""
    params = urllib.parse.urlencode({"query": query, "time": at.timestamp()})
    url = f"{runner.PROMETHEUS_URL.rstrip('/')}/api/v1/query?{params}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(runner._prometheus_request(url), timeout=120) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"respons Prometheus gagal: {payload}")
    return [(s["metric"], float(s["value"][1])) for s in payload["data"]["result"]]


def _session_node(pod, at):
    """Node tempat Pod berjalan (kube-state-metrics); None bila tidak tercatat."""
    result = _query_instant(
        f'kube_pod_info{{namespace="{runner.NAMESPACE}",pod="{pod}",'
        f'{runner.PROM_SERIES_FILTER}}}', at)
    return result[0][0].get("node") if result else None


def fetch_session(session_id, grid):
    """Nilai semua SUPPORTING_QUERIES pada titik grid sesi (list datetime)."""
    pod = runner._pod_name_from_session(session_id)
    base = f'k8s_namespace_name="{runner.NAMESPACE}",k8s_pod_name="{pod}",pipeline="otel"'
    selectors = {
        "pod": base,
        "job": base + ',k8s_container_name="job"',
        "sidecar": base + ',k8s_container_name="telemetry"',
        "node": _session_node(pod, grid[len(grid) // 2]) or "",
        "w": RATE_WINDOW,
    }
    step = (grid[1] - grid[0]).total_seconds() if len(grid) > 1 else 5.0
    series = {name: runner._prometheus_query_range(template % selectors, grid[0], grid[-1], step)
              for name, template in SUPPORTING_QUERIES.items()}
    rows = []
    for ts in grid:
        row = {"timestamp": ts.isoformat(), "session_id": session_id}
        for name in SUPPORTING_COLUMNS:
            value = series[name].get(ts.timestamp())
            row[name] = round(value, 6) if value is not None else None
        rows.append(row)
    return rows


def fetch_netobserv_flows(start, end, out_path):
    """Flow dan byte NetObserv per pasangan namespace selama [start, end]."""
    window = f"{int((end - start).total_seconds())}s"
    by = "sum by (SrcK8S_Namespace, DstK8S_Namespace)"
    table = {}
    for column, metric in (("flows", "netobserv_namespace_flows_total"),
                           ("ingress_bytes", "netobserv_workload_ingress_bytes_total")):
        for labels, value in _query_instant(f"{by} (increase({metric}[{window}]))", end):
            key = (labels.get("SrcK8S_Namespace", ""), labels.get("DstK8S_Namespace", ""))
            table.setdefault(key, {})[column] = round(value, 1)
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["src_namespace", "dst_namespace", "flows", "ingress_bytes"])
        for (src, dst), values in sorted(table.items(), key=lambda kv: -kv[1].get("flows", 0)):
            writer.writerow([src, dst, values.get("flows", 0), values.get("ingress_bytes", 0)])
    return table


def main(out_dir):
    with open(os.path.join(out_dir, "plan.json")) as f:
        plan = json.load(f)
    rows, missing = [], []
    for index, entry in enumerate(plan["sessions"], start=1):
        session_id = entry["session_id"]
        prom_path = os.path.join(out_dir, f"{session_id}.prometheus.csv")
        if not os.path.exists(prom_path):
            missing.append(session_id)
            continue
        grid = [datetime.fromisoformat(t).astimezone(timezone.utc)
                for t in pd.read_csv(prom_path, usecols=["timestamp"])["timestamp"]]
        rows += fetch_session(session_id, grid)
        if index % 25 == 0:
            print(f"[fetch_supporting] {index}/{len(plan['sessions'])} sesi")
    if missing:
        print(f"[fetch_supporting] {len(missing)} sesi dilewati (tanpa .prometheus.csv): {missing}")

    supporting = pd.DataFrame(rows, columns=["timestamp", "session_id"] + SUPPORTING_COLUMNS)
    supporting_path = os.path.join(out_dir, "supporting_metrics.csv")
    supporting.to_csv(supporting_path, index=False)

    # Gabung ke dataset.csv seperti runner menggabung kolom prom_*: sampel terdekat
    dataset = pd.read_csv(os.path.join(out_dir, "dataset.csv"))
    interval = float(plan["args"]["interval_sec"])
    left = dataset.assign(_ts=pd.to_datetime(dataset["timestamp"], utc=True)
                          .astype("datetime64[us, UTC]")).sort_values(["_ts", "session_id"])
    right = supporting.drop(columns="timestamp").assign(
        _ts=pd.to_datetime(supporting["timestamp"], utc=True)
        .astype("datetime64[us, UTC]")).sort_values(["_ts", "session_id"])
    extended = pd.merge_asof(left, right, on="_ts", by="session_id", direction="nearest",
                             tolerance=pd.Timedelta(seconds=max(interval, 5)))
    extended = extended.set_index(left.index).loc[dataset.index].drop(columns="_ts")
    extended_path = os.path.join(out_dir, "dataset_extended.csv")
    extended.to_csv(extended_path, index=False)

    timestamps = pd.to_datetime(dataset["timestamp"], utc=True)
    flows_path = os.path.join(out_dir, "supporting_netobserv_flows.csv")
    flows = fetch_netobserv_flows(timestamps.min().to_pydatetime(),
                                  timestamps.max().to_pydatetime(), flows_path)
    testbed = sum(v.get("flows", 0) for (src, dst), v in flows.items()
                  if runner.NAMESPACE in (src, dst))

    coverage = supporting[SUPPORTING_COLUMNS].notna().mean().mul(100).round(1)
    print(f"[fetch_supporting] {supporting_path}: {len(supporting)} baris, "
          f"{supporting['session_id'].nunique()} sesi")
    print(f"[fetch_supporting] {extended_path}: {len(extended)} baris, {extended.shape[1]} kolom")
    print(f"[fetch_supporting] {flows_path}: {len(flows)} pasangan namespace; "
          f"flow yang melibatkan {runner.NAMESPACE}: {testbed:.0f}")
    print("[fetch_supporting] % titik grid terisi per kolom:")
    print(coverage.to_string())


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("pemakaian: python fetch_supporting_metrics.py <out_dir>")
    main(sys.argv[1])
