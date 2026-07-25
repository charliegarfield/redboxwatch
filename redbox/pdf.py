"""PDF text extraction and page rendering (spec §3.3, §3.4, §5).

Uses pdfminer.six for text. Red boxes are sometimes posted as linked PDFs, so
PDF text is a first-class input to the classifier.

Rendering uses pypdfium2: pages are rasterized and stacked into one tall PNG so
a PDF-sourced detection gets the same visual exhibit as a full-page screenshot
of an HTML page (the archiver transcodes it to WebP like any screenshot).
"""
from __future__ import annotations

import io

from pdfminer.high_level import extract_text


def extract_pdf_text(data: bytes) -> str:
    """Extract text from PDF bytes. Returns '' on failure (e.g. scanned image)."""
    try:
        return extract_text(io.BytesIO(data)) or ""
    except Exception:
        return ""


def render_pdf_png(data: bytes, *, max_pages: int = 10, scale: float = 2.0) -> bytes | None:
    """Rasterize up to *max_pages* pages of a PDF into one vertically stacked PNG.

    Returns None on any failure (corrupt file, encrypted PDF, missing deps) —
    the caller archives the raw PDF and text regardless, so a render miss only
    costs the visual exhibit, never the evidence.
    """
    try:
        import pypdfium2 as pdfium
        from PIL import Image

        doc = pdfium.PdfDocument(io.BytesIO(data))
        try:
            n = min(len(doc), max_pages)
            if n == 0:
                return None
            pages = []
            for i in range(n):
                page = doc[i]
                pages.append(page.render(scale=scale).to_pil().convert("RGB"))
                page.close()
            width = max(p.width for p in pages)
            gap = 8  # thin separator between pages so boundaries stay visible
            height = sum(p.height for p in pages) + gap * (len(pages) - 1)
            sheet = Image.new("RGB", (width, height), (229, 229, 229))
            y = 0
            for p in pages:
                sheet.paste(p, ((width - p.width) // 2, y))
                y += p.height + gap
            buf = io.BytesIO()
            sheet.save(buf, format="PNG")
            return buf.getvalue()
        finally:
            doc.close()
    except Exception:
        return None
