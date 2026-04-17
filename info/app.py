import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from flask import Flask
import requests as req

app = Flask(__name__)

REGISTRY_URL = "http://registry:5000"
CACHE_PATH = "/var/lib/registry"


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


@app.get("/_info")
def info():
    checks = {}
    check_fns = {"registry": _check_registry, "upstream": _check_upstream, "disk": _check_disk}
    with ThreadPoolExecutor(max_workers=3) as ex:
        futures = {ex.submit(fn): name for name, fn in check_fns.items()}
        for future in as_completed(futures, timeout=0.9):
            name = futures[future]
            try:
                checks[name] = future.result()
            except Exception as e:
                checks[name] = {"ok": False, "techDetail": str(e)}
    return {
        "system": _env("SYSTEM", "lucos_docker_mirror"),
        "checks": checks,
        "metrics": {},
        "ci": {"circle": "gh/lucas42/lucos_docker_mirror"},
        "title": "Docker Mirror",
    }


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000)
