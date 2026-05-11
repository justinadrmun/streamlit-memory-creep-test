import os
import threading
import time
import csv
from datetime import datetime


def get_rss_mb() -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024.0
    return 0.0


def get_vms_mb() -> float:
    with open("/proc/self/status") as f:
        for line in f:
            if line.startswith("VmSize:"):
                return int(line.split()[1]) / 1024.0
    return 0.0


class RSSLogger:
    def __init__(self, csv_path: str = "/app/results/memory_log.csv", interval: float = 1.0):
        self.csv_path = csv_path
        self.interval = interval
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._events = {}

        os.makedirs(os.path.dirname(csv_path), exist_ok=True)

    def mark_event(self, name: str):
        with self._lock:
            self._events[name] = time.time()

    def start(self):
        with open(self.csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "elapsed_s", "rss_mb", "vms_mb", "event"])

        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self):
        t0 = time.time()
        while not self._stop.is_set():
            elapsed = time.time() - t0
            rss = get_rss_mb()
            vms = get_vms_mb()

            event = ""
            with self._lock:
                for name, ts in list(self._events.items()):
                    if abs(time.time() - ts) < (self.interval * 2):
                        event = name
                        break

            with open(self.csv_path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    datetime.now().isoformat(),
                    f"{elapsed:.1f}",
                    f"{rss:.2f}",
                    f"{vms:.2f}",
                    event,
                ])

            time.sleep(self.interval)


_logger_instance = None


def get_logger(csv_path: str = "/app/results/memory_log.csv") -> RSSLogger:
    global _logger_instance
    if _logger_instance is None:
        _logger_instance = RSSLogger(csv_path=csv_path)
        _logger_instance.start()
    return _logger_instance
