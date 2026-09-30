import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_tmp = Path(tempfile.mkdtemp(prefix="nova_test_"))
os.environ["NOVA_DB_PATH"] = str(_tmp / "test.db")
os.environ["NOVA_CHECKPOINT_PATH"] = str(_tmp / "ckpt.db")
os.environ["FALLBACK_MODEL"] = "fake/strong-vision"
sys.path.insert(0, str(ROOT))

if not (ROOT / "data" / "samples" / "invoice_clean.pdf").exists():
    import subprocess
    subprocess.run([sys.executable, str(ROOT / "scripts" / "generate_samples.py")], check=True)
