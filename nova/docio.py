"""Document ingestion: PDF/image -> page PNGs on disk + best available text layer.
Page images are stored as file paths (not base64) so checkpoints stay small."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

import pypdfium2 as pdfium
from PIL import Image, ImageOps

log = logging.getLogger("nova.docio")
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}


def file_id(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


def _ocr(img: Image.Image) -> Optional[str]:
    """Optional OCR. Needs `pip install pytesseract` + tesseract binary. Silent no-op otherwise."""
    try:
        import pytesseract  # type: ignore
        return pytesseract.image_to_string(img)
    except Exception:
        return None


def load_document(path: str | Path, out_dir: Path, dpi: int, max_pages: int) -> dict:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    ext = path.suffix.lower()
    pages: list[str] = []
    texts: list[str] = []
    warnings: list[str] = []
    n_pages = 1

    if ext == ".pdf":
        pdf = pdfium.PdfDocument(str(path))
        n_pages = len(pdf)
        if n_pages > max_pages:
            warnings.append(f"Document has {n_pages} pages; only first {max_pages} processed.")
        for i in range(min(n_pages, max_pages)):
            page = pdf[i]
            img = page.render(scale=dpi / 72).to_pil().convert("RGB")
            p = out_dir / f"page_{i + 1}_{dpi}dpi.png"
            img.save(p)
            pages.append(str(p))
            texts.append(page.get_textpage().get_text_range() or "")
        pdf.close()
    elif ext in IMAGE_EXT:
        img = ImageOps.exif_transpose(Image.open(path)).convert("RGB")
        # upscale small scans a bit so the vision model has pixels to work with
        scale = dpi / 150
        if scale != 1:
            img = img.resize((int(img.width * scale), int(img.height * scale)))
        p = out_dir / f"page_1_{dpi}dpi.png"
        img.save(p)
        pages.append(str(p))
        texts.append("")
    else:
        raise ValueError(f"Unsupported file type: {ext}")

    text = "\n".join(texts).strip()
    source = "pdf_text_layer" if len(text) >= 60 else None
    if source is None:
        ocr_texts = [t for t in (_ocr(Image.open(p)) for p in pages) if t]
        if ocr_texts and len("".join(ocr_texts)) >= 60:
            text, source = "\n".join(ocr_texts), "ocr"
        else:
            text = ""
            warnings.append("No text layer and no OCR available: extracted values cannot be "
                            "grounded against the source, so they are capped below the approval threshold.")
    return {"path": str(path), "filename": path.name, "pages": pages, "n_pages": n_pages,
            "text": text, "text_source": source, "warnings": warnings, "dpi": dpi}
