"""
data_generator/k8s_job_runner.py
===================================
Pengganti job_runner.py untuk eksekusi di Kubernetes/kind -- menerima
objek Scenario yang SAMA PERSIS dari registry (scenario_base.py TIDAK
BERUBAH). Submit Pod 2-container (job + sidecar telemetri, berbagi PID
namespace), tunggu selesai, tarik CSV keluar via `kubectl exec`, hapus Pod,
lalu ambil metrik Prometheus sesi tersebut lewat query_range dari host.

GPU OPSIONAL: default tanpa GPU. Set K8S_REQUEST_GPU=true (dan JOB_IMAGE
ke image torch CUDA) untuk menambahkan `resources.limits:
{"nvidia.com/gpu": 1}` ke container job dan memberi sidecar akses
nvidia-smi. Cluster tanpa NVIDIA device plugin tidak mengiklankan
resource tersebut -- Pod yang memintanya akan macet Pending selamanya.
JANGAN aktifkan kecuali device plugin sudah dipasang & diverifikasi
(k0s-cluster/test-gpu.sh).

Prasyarat: pip install kubernetes
           kubeconfig sudah menunjuk ke cluster kind aktif
"""

import csv
import json
import os
import random
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import pandas as pd
from kubernetes import client, config

from scenario_base import Scenario

NAMESPACE = os.environ.get("K8S_NAMESPACE", "testbed-jobs")
JOB_IMAGE = os.environ.get("JOB_IMAGE", "local/data-generator-job:latest")
IMAGE_PULL_POLICY = os.environ.get("IMAGE_PULL_POLICY", "IfNotPresent")
REQUEST_GPU = os.environ.get("K8S_REQUEST_GPU", "false").lower() == "true"

# Diakses dari HOST (runner), bukan dari Pod -- URL ingress sslip.io cocok.
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.127.0.0.1.sslip.io")
PROMETHEUS_TIMEOUT_SEC = float(os.environ.get("PROMETHEUS_TIMEOUT_SEC", "10"))
# Jendela rate(); minimal 2x scrape interval cAdvisor (10s di kube-prometheus-stack).
PROM_RATE_WINDOW = os.environ.get("PROM_RATE_WINDOW", "1m")
# Tunggu sampel scrape terakhir masuk ke Prometheus sebelum query_range.
PROM_SCRAPE_LAG_SEC = float(os.environ.get("PROM_SCRAPE_LAG_SEC", "25"))
# File heartbeat aktivitas notebook di volume bersama (scenario_base.touch_activity)
ACTIVITY_FILE = "/data/last_activity"

# %(job)s = hanya container job (sidecar telemetri tidak ikut terhitung).
# %(pod)s = level Pod; metrik network memang tidak punya label container.
PROMETHEUS_QUERIES = {
    "cpu_pct": '100 * sum(rate(container_cpu_usage_seconds_total{%(job)s}[%(w)s]))',
    "cpu_cores": 'sum(rate(container_cpu_usage_seconds_total{%(job)s}[%(w)s]))',
    "mem_rss_mb": 'sum(container_memory_working_set_bytes{%(job)s}) / 1024 / 1024',
    "mem_usage_mb": 'sum(container_memory_usage_bytes{%(job)s}) / 1024 / 1024',
    "mem_rss_prom_mb": 'sum(container_memory_rss{%(job)s}) / 1024 / 1024',
    "io_read_mb": 'sum(rate(container_fs_reads_bytes_total{%(job)s}[%(w)s])) / 1024 / 1024',
    "io_write_mb": 'sum(rate(container_fs_writes_bytes_total{%(job)s}[%(w)s])) / 1024 / 1024',
    "net_rx_mb": 'sum(rate(container_network_receive_bytes_total{%(pod)s}[%(w)s])) / 1024 / 1024',
    "net_tx_mb": 'sum(rate(container_network_transmit_bytes_total{%(pod)s}[%(w)s])) / 1024 / 1024',
    "net_rx_packets": 'sum(rate(container_network_receive_packets_total{%(pod)s}[%(w)s]))',
    "net_tx_packets": 'sum(rate(container_network_transmit_packets_total{%(pod)s}[%(w)s]))',
    "restarts_total": 'sum(kube_pod_container_status_restarts_total{%(job)s})',
    "cpu_request_cores": 'sum(kube_pod_container_resource_requests{%(job)s,resource="cpu",unit="core"})',
    "cpu_limit_cores": 'sum(kube_pod_container_resource_limits{%(job)s,resource="cpu",unit="core"})',
    "mem_request_mb": 'sum(kube_pod_container_resource_requests{%(job)s,resource="memory",unit="byte"}) / 1024 / 1024',
    "mem_limit_mb": 'sum(kube_pod_container_resource_limits{%(job)s,resource="memory",unit="byte"}) / 1024 / 1024',
    "pod_running": 'max(kube_pod_status_phase{%(pod)s,phase="Running"})',
    # Konteks cluster (Kajian CATS): antrean GPU untuk R1/P5, sesi per
    # pengguna untuk R6. Waiter/sibling dibuat runner -- lihat run_labeled_job.
    "queue_length": (
        'count(kube_pod_status_phase{%(ns)s,pod=~"waiter-.*",phase="Pending"} == 1)'
        ' or vector(0)'
    ),
    "queue_wait_max_sec": (
        'max((time() - kube_pod_created{%(ns)s,pod=~"waiter-.*"}) and on(pod)'
        ' (kube_pod_status_phase{%(ns)s,pod=~"waiter-.*",phase="Pending"} == 1))'
        ' or vector(0)'
    ),
    "user_active_sessions": (
        'count(kube_pod_status_phase{%(ns)s,pod=~"%(podname)s(-sib-[0-9]+)?",'
        'phase="Running"} == 1) or vector(0)'
    ),
}
PROMETHEUS_COLUMNS = [f"prom_{name}" for name in PROMETHEUS_QUERIES]

_config_loaded = False


def _ensure_kube_config():
    global _config_loaded
    if not _config_loaded:
        config.load_kube_config()
        _config_loaded = True


def _kubectl_copy_file(pod_name, container, src_in_pod, dst_local):
    # `kubectl cp` requires `tar` inside the container, which is not
    # guaranteed by python:* -slim images.
    result = subprocess.run(
        ["kubectl", "exec", "-n", NAMESPACE, pod_name, "-c", container,
         "--", "cat", src_in_pod],
        capture_output=True,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, result.args, result.stdout, result.stderr,
        )
    with open(dst_local, "wb") as output:
        output.write(result.stdout)


def _prometheus_query_range(query, start, end, step_sec):
    params = urllib.parse.urlencode({
        "query": query, "start": start.timestamp(), "end": end.timestamp(),
        "step": step_sec,
    })
    url = f"{PROMETHEUS_URL.rstrip('/')}/api/v1/query_range?{params}"
    # URL lokal (127.0.0.1) -- jangan lewat HTTP(S)_PROXY dari shell
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=PROMETHEUS_TIMEOUT_SEC) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"respons Prometheus gagal: {payload}")
    results = payload.get("data", {}).get("result", [])
    if not results:
        return {}
    return {float(ts): float(value) for ts, value in results[0]["values"]}


def _fetch_prometheus_telemetry(session_id, pod_name, start, end, step_sec,
                                out_path):
    """Tulis <session_id>.prometheus.csv; return jumlah baris (0 = kosong)."""
    selectors = {
        "job": f'namespace="{NAMESPACE}",pod="{pod_name}",container="job"',
        "pod": f'namespace="{NAMESPACE}",pod="{pod_name}"',
        "ns": f'namespace="{NAMESPACE}"',
        "podname": pod_name,
        "w": PROM_RATE_WINDOW,
    }
    series = {}
    for name, template in PROMETHEUS_QUERIES.items():
        series[name] = _prometheus_query_range(
            template % selectors, start, end, step_sec,
        )

    timestamps = sorted(set().union(*(s.keys() for s in series.values())))
    with open(out_path, "w", newline="") as output:
        writer = csv.DictWriter(
            output, fieldnames=["timestamp", "session_id"] + PROMETHEUS_COLUMNS,
        )
        writer.writeheader()
        for ts in timestamps:
            row = {
                "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                "session_id": session_id,
            }
            for name, values in series.items():
                value = values.get(ts)
                row[f"prom_{name}"] = round(value, 6) if value is not None else None
            writer.writerow(row)
    return len(timestamps)


def _merge_telemetry_files(local_path, prometheus_path, merged_path,
                           tolerance_sec):
    local_df = pd.read_csv(local_path)
    if os.path.exists(prometheus_path) and os.path.getsize(prometheus_path) > 0:
        prometheus_df = pd.read_csv(prometheus_path)
    else:
        prometheus_df = pd.DataFrame(columns=["timestamp", "session_id"] + PROMETHEUS_COLUMNS)
    if local_df.empty:
        return False

    # merge_asof butuh resolusi waktu yang sama di kedua sisi
    local_df["timestamp"] = pd.to_datetime(
        local_df["timestamp"], utc=True,
    ).astype("datetime64[us, UTC]")
    prometheus_df["timestamp"] = pd.to_datetime(
        prometheus_df["timestamp"], utc=True,
    ).astype("datetime64[us, UTC]")
    prometheus_df["session_id"] = prometheus_df["session_id"].astype(local_df["session_id"].dtype)
    prometheus_df[PROMETHEUS_COLUMNS] = prometheus_df[PROMETHEUS_COLUMNS].astype(float)
    local_df = local_df.sort_values(["timestamp", "session_id"])
    prometheus_df = prometheus_df.sort_values(["timestamp", "session_id"])
    # Kolom prom_* kosong pada baris yang tidak punya sampel Prometheus terdekat
    merged = pd.merge_asof(
        local_df,
        prometheus_df,
        on="timestamp",
        by="session_id",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=tolerance_sec),
    )
    merged["timestamp"] = merged["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    merged.to_csv(merged_path, index=False)
    return True


def _pod_name_from_session(session_id: str) -> str:
    # Nama Pod harus DNS-1123: lowercase, "_" tidak diizinkan -> ganti "-"
    return f"session-{session_id}".replace("_", "-").lower()


def build_pod_manifest(scenario: Scenario, session_id: str, args: list,
                        interval_sec: float, extra_meta: dict,
                        request_gpu: bool = False):
    job_cmd_match = os.path.basename(scenario.script_path)

    job_resources = {kind: dict(values) for kind, values in scenario.job_resources.items()}
    if request_gpu:
        # Hanya aktif jika device plugin GPU sudah terpasang -- lihat
        # docstring modul ini. Default False untuk cluster tanpa GPU.
        job_resources.setdefault("limits", {})["nvidia.com/gpu"] = "1"

    job_container = {
        "name": "job",
        "image": JOB_IMAGE,
        "imagePullPolicy": IMAGE_PULL_POLICY,
        "command": ["python", scenario.script_path] + args,
        "env": [{"name": "ACTIVITY_FILE", "value": ACTIVITY_FILE}],
        "volumeMounts": [{"name": "shared-data", "mountPath": "/data"}],
        "resources": job_resources,
    }

    telemetry_container = {
        "name": "telemetry",
        "image": JOB_IMAGE,
        "imagePullPolicy": IMAGE_PULL_POLICY,
        "command": ["python", "/app/telemetry_sidecar.py"],
        "env": [
            {"name": "SESSION_ID", "value": session_id},
            {"name": "LABEL", "value": scenario.label},
            {"name": "JOB_TYPE", "value": scenario.name},
            {"name": "JOB_CMD_MATCH", "value": job_cmd_match},
            {"name": "INTERVAL_SEC", "value": str(interval_sec)},
            {"name": "GPU_REQUESTED", "value": "1" if request_gpu else "0"},
            {"name": "ACTIVITY_FILE", "value": ACTIVITY_FILE},
            {"name": "EXTRA_META_JSON", "value": json.dumps(extra_meta or {})},
        ],
        "volumeMounts": [{"name": "shared-data", "mountPath": "/data"}],
    }
    if request_gpu:
        # Sidecar tidak meminta nvidia.com/gpu (GPU hanya 1, dipegang job),
        # tapi runtime NVIDIA tetap menyuntikkan nvidia-smi lewat env ini.
        telemetry_container["env"] += [
            {"name": "NVIDIA_VISIBLE_DEVICES", "value": "all"},
            {"name": "NVIDIA_DRIVER_CAPABILITIES", "value": "utility"},
        ]

    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": _pod_name_from_session(session_id),
            "namespace": NAMESPACE,
            "labels": {"session_id": session_id, "label": scenario.label,
                        "job_type": scenario.name, "role": "session"},
        },
        "spec": {
            "shareProcessNamespace": True,
            "restartPolicy": "Never",
            "containers": [job_container, telemetry_container],
            "volumes": [{"name": "shared-data", "emptyDir": {}}],
        },
    }


def _helper_pod_manifest(name, session_id, role, command, resources=None,
                         node_name=None):
    """Pod pendukung konteks: 'waiter' (job GPU pengguna lain yang antre)
    atau 'sibling' (sesi notebook paralel milik pengguna yang sama)."""
    manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "namespace": NAMESPACE,
            "labels": {"session_id": session_id, "role": role},
        },
        "spec": {
            "restartPolicy": "Never",
            "terminationGracePeriodSeconds": 0,
            "containers": [{
                "name": role,
                "image": JOB_IMAGE,
                "imagePullPolicy": IMAGE_PULL_POLICY,
                "command": command,
                "resources": resources or {},
            }],
        },
    }
    if node_name:
        # JOB_IMAGE belum tentu ada di semua node (image GPU hanya di node GPU)
        manifest["spec"]["nodeSelector"] = {"kubernetes.io/hostname": node_name}
    return manifest


def _expected_duration(scenario, args):
    if "--duration-sec" in args:
        return float(args[args.index("--duration-sec") + 1])
    return float(scenario.timeout_sec or 600)


def _delete_pods(v1, names):
    for name in names:
        try:
            v1.delete_namespaced_pod(name, NAMESPACE, grace_period_seconds=0)
        except client.ApiException:
            pass


def run_labeled_job(scenario: Scenario, session_id: str, out_dir="data",
                     interval_sec=5.0, args_override=None, extra_meta=None,
                     request_gpu=None):
    """
    Signature SAMA PERSIS dengan job_runner.run_labeled_job() (versi
    subprocess lokal) -- run_experiment.py bisa memakai salah satu
    tanpa mengubah cara pemanggilan, hanya beda modul yang di-import.
    """
    _ensure_kube_config()
    v1 = client.CoreV1Api()
    if request_gpu is None:
        request_gpu = REQUEST_GPU

    args = args_override if args_override is not None else scenario.default_args
    timeout_sec = scenario.timeout_sec or 600

    pod_manifest = build_pod_manifest(
        scenario, session_id, args, interval_sec, extra_meta, request_gpu,
    )
    pod_name = pod_manifest["metadata"]["name"]

    # Waiter hanya bermakna di mode GPU: mereka Pending karena GPU satu-
    # satunya dipegang sesi ini. Tanpa GPU, waiter justru akan mendapat GPU.
    n_waiters = random.randint(*scenario.queue_waiters) if request_gpu else 0
    if scenario.queue_waiters[1] > 0 and not request_gpu:
        print(f"[k8s_job_runner] NOTE: antrean {scenario.name} dilewati "
              f"(butuh K8S_REQUEST_GPU=true)")
    expected_sec = _expected_duration(scenario, args)
    if scenario.queue_arrival == "spread":
        waiter_offsets = [expected_sec * (i + 1) / (n_waiters + 1) for i in range(n_waiters)]
    else:
        waiter_offsets = [0.0] * n_waiters
    helper_pods = []

    v1.create_namespaced_pod(namespace=NAMESPACE, body=pod_manifest)
    print(f"[k8s_job_runner] Pod {pod_name} dibuat (scenario={scenario.name}, "
          f"waiters={n_waiters}, siblings={scenario.sibling_sessions}), "
          f"menunggu job selesai (timeout={timeout_sec}s)...")

    start = time.time()
    job_state = None
    pod = None
    running_since = None
    try:
        while time.time() - start < timeout_sec + 30:  # +30s toleransi start-up image
            try:
                pod = v1.read_namespaced_pod_status(pod_name, NAMESPACE)
            except client.ApiException as e:
                print(f"[k8s_job_runner] gagal baca status Pod: {e}")
                break
            statuses = {c.name: c.state for c in (pod.status.container_statuses or [])}
            state = statuses.get("job")
            if state and state.terminated is not None:
                job_state = state.terminated
                break
            if running_since is None and state and state.running is not None:
                # Pod pendukung dibuat setelah sesi memegang GPU-nya
                running_since = time.time()
                for i in range(scenario.sibling_sessions):
                    name = f"{pod_name}-sib-{i + 1}"
                    v1.create_namespaced_pod(namespace=NAMESPACE, body=_helper_pod_manifest(
                        name, session_id, "sibling",
                        ["python", "scenarios/benign_eda_notebook.py",
                         "--duration-sec", str(int(expected_sec) + 60), "--rows", "50000"],
                        node_name=pod.spec.node_name,
                    ))
                    helper_pods.append(name)
            if running_since is not None:
                while waiter_offsets and time.time() - running_since >= waiter_offsets[0]:
                    waiter_offsets.pop(0)
                    name = f"waiter-{pod_name[len('session-'):]}-{len(helper_pods) + 1}"
                    v1.create_namespaced_pod(namespace=NAMESPACE, body=_helper_pod_manifest(
                        name, session_id, "waiter", ["sleep", "infinity"],
                        {"limits": {"nvidia.com/gpu": "1"}},
                    ))
                    helper_pods.append(name)
            time.sleep(3)
    except BaseException:
        # Ctrl-C / error: jangan tinggalkan Pod sesi yang memegang GPU
        _delete_pods(v1, helper_pods + [pod_name])
        raise
    # Hapus waiter sebelum GPU dilepas, agar waiter tidak sempat berjalan
    _delete_pods(v1, helper_pods)

    if job_state is None:
        print(f"[k8s_job_runner] TIMEOUT/gagal -- job {session_id} tidak "
              f"selesai dalam batas waktu.")
        job_start = (pod.status.start_time if pod is not None and pod.status.start_time
                     else datetime.fromtimestamp(start, timezone.utc))
        job_end = datetime.now(timezone.utc)
    else:
        job_start, job_end = job_state.started_at, job_state.finished_at
        if job_state.exit_code != 0:
            print(f"[k8s_job_runner] WARNING: job {session_id} exit "
                  f"code={job_state.exit_code} reason={job_state.reason}")

    # Tunggu sidecar menulis penanda /data/DONE (tanda CSV siap diambil)
    done_deadline = time.time() + max(interval_sec * 3, 15)
    while time.time() < done_deadline:
        check = subprocess.run(
            ["kubectl", "exec", "-n", NAMESPACE, pod_name, "-c", "telemetry",
             "--", "test", "-f", "/data/DONE"],
            capture_output=True,
        )
        if check.returncode == 0:
            break
        time.sleep(2)

    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, f"{session_id}.csv")
    prometheus_path = os.path.join(out_dir, f"{session_id}.prometheus.csv")
    merged_path = os.path.join(out_dir, f"{session_id}.merged.csv")
    try:
        _kubectl_copy_file(pod_name, "telemetry", "/data/telemetry.csv",
                           out_path)
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or b"").decode(errors="replace").strip()
        print(f"[k8s_job_runner] WARNING: gagal mengambil CSV telemetri dari "
              f"{pod_name}: {detail or 'kubectl exec gagal'}")
        out_path = None

    try:
        v1.delete_namespaced_pod(pod_name, NAMESPACE, grace_period_seconds=0)
    except client.ApiException:
        pass

    # Data Pod tetap tersimpan di Prometheus setelah Pod dihapus.
    lag = PROM_SCRAPE_LAG_SEC - (datetime.now(timezone.utc) - job_end).total_seconds()
    if lag > 0:
        time.sleep(lag)
    try:
        n_rows = _fetch_prometheus_telemetry(
            session_id, pod_name, job_start, job_end, interval_sec,
            prometheus_path,
        )
        if n_rows == 0:
            print(f"[k8s_job_runner] WARNING: Prometheus tidak punya data "
                  f"untuk {pod_name}; kolom prom_* akan kosong")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[k8s_job_runner] WARNING: gagal query Prometheus "
              f"({PROMETHEUS_URL}): {exc}")

    if out_path is not None:
        _merge_telemetry_files(out_path, prometheus_path, merged_path,
                               tolerance_sec=max(interval_sec, 5))

    print(f"[k8s_job_runner] session={session_id} scenario={scenario.name} "
          f"label={scenario.label} -> {out_path}")
    return out_path
