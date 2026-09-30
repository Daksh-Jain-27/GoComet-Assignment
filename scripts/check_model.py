"""Check that a model answers, and can read an image, with your key.
  python scripts/check_model.py gemini/<model-name>
"""
import _path  # noqa: F401
import sys
import time

import litellm

from nova.config import SETTINGS
from nova.docio import load_document
from nova.llm import image_part

model = sys.argv[1]
doc = load_document(SETTINGS.samples_dir / "invoice_errors.pdf", SETTINGS.runs_dir / "_check", 100, 1)
t0 = time.perf_counter()
try:
    r = litellm.completion(model=model, max_tokens=8000, messages=[{"role": "user", "content": [
        {"type": "text", "text": "What is the HS Code printed on this document? Reply with just the code."},
        image_part(doc["pages"][0])]}])
    print(f"OK  {model}  {time.perf_counter() - t0:.1f}s  answer: {r.choices[0].message.content!r}")
    print("Expected answer: 8528.72.00")
except Exception as e:
    print(f"FAIL {model}  {type(e).__name__}: {str(e)[:300]}")