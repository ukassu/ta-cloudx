"""
detector/k8s_integration/daemon_scorer/main.py
=================================================
Pola integrasi: POLLING PERIODIK. Berjalan sebagai DaemonSet (1
instance per node) atau Deployment (terpusat) -- pilih sesuai
kebutuhan riset. Secara berkala membaca metrik Pod dan memberi skor
anomali, lalu menulis hasilnya sebagai label/annotation pada Pod
tersebut.

GENERIK terhadap model: model dimuat lewat BaseDetector.load(), skor
dihitung lewat model.score() -- kode ini tidak berubah apapun model
yang dipakai (Isolation Forest, LSTM-AE, clustering, ensemble, dst),
selama subclass BaseDetector diimplementasikan dengan benar.

Cocok dipakai untuk: deteksi real-time dengan latensi longgar (siklus
polling detik-menit), TIDAK memblokir penjadwalan pod (beda dari pola
webhook_server/).
"""

import importlib
import json
import os
import time
import urllib.parse
import urllib.request

import numpy as np
from kubernetes import client, config

DETECTOR_CLASS_PATH = os.environ["DETECTOR_CLASS_PATH"]  # mis. "my_model.MyDetector"
MODEL_PATH = os.environ.get("MODEL_PATH", "/model/model.bin")
FEATURE_MODE = os.environ.get("FEATURE_MODE", "aggregate")  # "aggregate" | "sequence"
THRESHOLD = float(os.environ.get("ANOMALY_THRESHOLD", "0.0"))
TARGET_NAMESPACE = os.environ.get("TARGET_NAMESPACE", "default")
NODE_NAME = os.environ.get("NODE_NAME")  # diisi Downward API jika mode DaemonSet
POLL_INTERVAL_SEC = float(os.environ.get("POLL_INTERVAL_SEC", "15"))
PROMETHEUS_URL = os.environ.get(
    "PROMETHEUS_URL",
    "http://prometheus-operated.monitoring.svc:9090",
).rstrip("/")
PROMETHEUS_WINDOW_SEC = int(os.environ.get("PROMETHEUS_WINDOW_SEC", "60"))
PROMETHEUS_STEP_SEC = int(os.environ.get("PROMETHEUS_STEP_SEC", "15"))
PROMETHEUS_TIMEOUT_SEC = float(os.environ.get("PROMETHEUS_TIMEOUT_SEC", "5"))

# Keep this order identical to the non-GPU columns used by
# common.feature_engineering.NUMERIC_TELEMETRY_COLUMNS_NO_GPU.
PROMETHEUS_QUERIES = {
    "cpu_pct": (
        '100 * sum by (namespace, pod) (rate('
        'container_cpu_usage_seconds_total{namespace="%s",pod="%s",'
        'container!="",container!="POD"}[5m]))'
    ),
    "mem_rss_mb": (
        'sum by (namespace, pod) (container_memory_working_set_bytes{'
        'namespace="%s",pod="%s",container!="",container!="POD"}) / 1024 / 1024'
    ),
    "io_read_mb": (
        'sum by (namespace, pod) (rate(container_fs_reads_bytes_total{'
        'namespace="%s",pod="%s",container!="",container!="POD"}[5m])) '
        '/ 1024 / 1024'
    ),
    "io_write_mb": (
        'sum by (namespace, pod) (rate(container_fs_writes_bytes_total{'
        'namespace="%s",pod="%s",container!="",container!="POD"}[5m])) '
        '/ 1024 / 1024'
    ),
}

ANOMALY_SCORE_ANNOTATION = "testbed.io/anomaly-score"
ANOMALY_FLAG_LABEL = "testbed.io/anomaly-flag"


def load_model():
    module_path, class_name = DETECTOR_CLASS_PATH.rsplit(".", 1)
    DetectorClass = getattr(importlib.import_module(module_path), class_name)
    return DetectorClass.load(MODEL_PATH)


def get_target_pods():
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    pods = client.CoreV1Api().list_namespaced_pod(
        TARGET_NAMESPACE, field_selector="status.phase=Running",
    ).items
    if NODE_NAME:
        pods = [pod for pod in pods if pod.spec.node_name == NODE_NAME]
    return pods


def _prometheus_query(query: str, *, start=None, end=None):
    endpoint = "/api/v1/query_range" if start is not None else "/api/v1/query"
    params = {"query": query}
    if start is not None:
        params.update({
            "start": str(start), "end": str(end),
            "step": str(PROMETHEUS_STEP_SEC),
        })
    url = f"{PROMETHEUS_URL}{endpoint}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=PROMETHEUS_TIMEOUT_SEC) as response:
            payload = json.load(response)
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Prometheus tidak dapat diakses di {PROMETHEUS_URL}: {exc}") from exc
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus mengembalikan respons gagal: {payload}")
    return payload["data"]["result"]


def _values(result, range_query):
    values = result[0].get("values" if range_query else "value", [])
    if range_query:
        values = [item[1] for item in values]
    else:
        values = [values[1]] if values else []
    try:
        return np.asarray([float(value) for value in values], dtype=np.float32)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Nilai metrik Prometheus bukan angka: {values}") from exc


def fetch_pod_features(pod):
    namespace, name = pod.metadata.namespace, pod.metadata.name
    end = time.time()
    start = end - PROMETHEUS_WINDOW_SEC
    metrics = {}
    range_query = FEATURE_MODE == "aggregate"
    for metric_name, template in PROMETHEUS_QUERIES.items():
        result = _prometheus_query(
            template % (namespace, name),
            start=start if range_query else None,
            end=end if range_query else None,
        )
        values = _values(result, range_query)
        if values.size == 0:
            raise RuntimeError(
                f"Prometheus tidak memiliki data {metric_name} untuk Pod "
                f"{namespace}/{name}"
            )
        metrics[metric_name] = values

    if FEATURE_MODE == "aggregate":
        # Same layout as aggregate_sessions(): mean, std, max, min per
        # metric, with metrics in NUMERIC_TELEMETRY_COLUMNS_NO_GPU order.
        return np.asarray([
            statistic
            for metric_name in PROMETHEUS_QUERIES
            for statistic in (
                metrics[metric_name].mean(),
                metrics[metric_name].std(),
                metrics[metric_name].max(),
                metrics[metric_name].min(),
            )
        ], dtype=np.float32)
    return np.asarray([metrics[name][-1] for name in PROMETHEUS_QUERIES], dtype=np.float32)


def label_pod(pod, score: float, is_anomaly: bool):
    v1 = client.CoreV1Api()
    v1.patch_namespaced_pod(
        pod.metadata.name,
        pod.metadata.namespace,
        {"metadata": {
            "annotations": {
                ANOMALY_SCORE_ANNOTATION: str(score),
            },
            "labels": {
                ANOMALY_FLAG_LABEL: "true" if is_anomaly else "false",
            },
        }},
    )


def main_loop():
    model = load_model()
    print(f"[daemon_scorer] model dimuat ({DETECTOR_CLASS_PATH}), "
          f"feature_mode={FEATURE_MODE}, namespace={TARGET_NAMESPACE}, "
          f"node={NODE_NAME or '(semua node)'}")

    while True:
        try:
            pods = get_target_pods()
            for pod in pods:
                features = fetch_pod_features(pod)
                score = float(model.score(features.reshape(1, -1))[0])
                is_anomaly = score > THRESHOLD
                label_pod(pod, score, is_anomaly)
        except Exception as e:
            print(f"[daemon_scorer] error pada siklus polling: {e}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    main_loop()
