import subprocess
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent


def run(*args):
    return subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_qr.py"), *args], capture_output=True, text=True)


def test_qr_generated_high_res(tmp_path):
    out = tmp_path / "q.png"
    r = run("--url", "https://attendance.example.com", "--out", str(out))
    assert r.returncode == 0, r.stderr
    assert Image.open(out).size[0] >= 1000


def test_qr_rejects_unsafe_urls(tmp_path):
    assert run("--url", "http://attendance.example.com", "--out", str(tmp_path / "a.png")).returncode == 2
    assert run("--url", "https://a.example.com/?emp=1", "--out", str(tmp_path / "b.png")).returncode == 2
    assert run("--url", "not a url", "--out", str(tmp_path / "c.png")).returncode == 2
