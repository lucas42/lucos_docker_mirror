import os
import stat
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, TimeoutError
from flask import Flask
import requests as req

app = Flask(__name__)

REGISTRY_URL = "http://registry:5000"
CACHE_PATH = "/var/lib/registry"
LOG_PATH = "/var/log/nginx/access.log"
PULL_WINDOW_SECONDS = 300  # 5 minutes


def _env(key, default=""):
    return os.environ.get(key, default)


def _check_registry():
    try:
        r = req.get(f"{REGISTRY_URL}/v2/", timeout=0.4)
        if r.status_code == 200:
            return {"ok": True, "techDetail": "Registry v2 API responding"}
        return {"ok": False, "techDetail": f"Registry returned HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "techDetail": f"Registry unreachable: {e}"}


def _check_upstream():
    try:
        r = req.get("https://registry-1.docker.io/v2/", timeout=0.4)
        if r.status_code in (200, 401):
            return {"ok": True, "techDetail": "Docker Hub reachable"}
        return {"ok": False, "techDetail": f"Docker Hub returned HTTP {r.status_code}"}
    except Exception as e:
        return {"ok": False, "techDetail": f"Docker Hub unreachable: {e}"}


def _check_disk():
    try:
        stat = os.statvfs(CACHE_PATH)
        total = stat.f_blocks * stat.f_frsize
        available = stat.f_bavail * stat.f_frsize
        used_pct = (1 - available / total) * 100 if total > 0 else 0
        used_gb = (total - available) / (1024 ** 3)
        detail = f"{used_pct:.0f}% used ({used_gb:.1f} GB)"
        if used_pct > 90:
            return {"ok": False, "techDetail": detail}
        return {"ok": True, "techDetail": detail}
    except Exception as e:
        return {"ok": False, "techDetail": str(e)}


def _metric_pull_rate():
    """Count blob GET requests served in the last 5 minutes from the nginx access log.

    Log format (set in nginx.conf.template): '<msec> "<method> <path> <proto>" <status>'
    Returns 0 gracefully if the log is absent, unreadable, or not a regular file
    (e.g. a symlink to /dev/stdout — a defensive guard against the symlink-trap incident).
    """
    cutoff = time.time() - PULL_WINDOW_SECONDS
    count = 0
    try:
        st = os.stat(LOG_PATH)
        if not stat.S_ISREG(st.st_mode):
            return 0
        with open(LOG_PATH) as f:
            for line in f:
                parts = line.split(" ", 1)
                if len(parts) < 2:
                    continue
                try:
                    ts = float(parts[0])
                except ValueError:
                    continue
                if ts < cutoff:
                    continue
                if '"GET /v2/' in parts[1] and "/blobs/" in parts[1]:
                    count += 1
    except (FileNotFoundError, OSError):
        return 0
    return count


@app.get("/_info")
def info():
    checks = {}
    check_fns = {"registry": _check_registry, "upstream": _check_upstream, "disk": _check_disk}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fn): name for name, fn in check_fns.items()}
        try:
            for future in as_completed(futures, timeout=0.9):
                name = futures[future]
                try:
                    checks[name] = future.result()
                except Exception as e:
                    checks[name] = {"ok": False, "techDetail": str(e)}
        except TimeoutError:
            for future, name in futures.items():
                if name not in checks:
                    checks[name] = {"ok": False, "techDetail": "check timed out"}
    return {
        "system": _env("SYSTEM", "lucos_docker_mirror"),
        "checks": checks,
        "metrics": {"docker_mirror_pull_count": _metric_pull_rate()},
        "ci": {"circle": "gh/lucas42/lucos_docker_mirror"},
        "title": "Docker Mirror",
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
