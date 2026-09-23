from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile

from docx import Document as WordDocument


def make_pdf_bytes(page_texts: list[str]) -> bytes:
    """Build a tiny deterministic text PDF without an extra fixture dependency."""
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    page_ids = []
    for index, text in enumerate(page_texts):
        page_id = 4 + index * 2
        stream_id = page_id + 1
        page_ids.append(f"{page_id} 0 R")
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
        objects[page_id] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>"
        ).encode("ascii")
        objects[stream_id] = (
            f"<< /Length {len(stream)} >>\nstream\n".encode("ascii") + stream + b"\nendstream"
        )
    objects[2] = (f"<< /Type /Pages /Kids [{' '.join(page_ids)}] /Count {len(page_ids)} >>").encode(
        "ascii"
    )

    result = bytearray(b"%PDF-1.4\n")
    offsets = [0] * (max(objects) + 1)
    for object_id in sorted(objects):
        offsets[object_id] = len(result)
        result.extend(f"{object_id} 0 obj\n".encode("ascii"))
        result.extend(objects[object_id])
        result.extend(b"\nendobj\n")
    xref_offset = len(result)
    result.extend(f"xref\n0 {len(offsets)}\n".encode("ascii"))
    result.extend(b"0000000000 65535 f \n")
    for object_id in range(1, len(offsets)):
        result.extend(f"{offsets[object_id]:010} 00000 n \n".encode("ascii"))
    result.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\n"
        f"startxref\n{xref_offset}\n%%EOF\n".encode("ascii")
    )
    return bytes(result)


def make_docx_bytes() -> bytes:
    """Create a small paragraph/table/paragraph Word file for extraction tests."""
    document = WordDocument()
    document.add_paragraph("DOCX paragraph before table")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "first cell"
    table.cell(0, 1).text = "second cell"
    document.add_paragraph("DOCX paragraph after table")
    output = BytesIO()
    document.save(output)
    return output.getvalue()


def make_zip_bytes(entries: dict[str, bytes]) -> bytes:
    """Create deterministic in-memory ZIPs for DOCX structure and expansion checks."""
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        for name, body in entries.items():
            archive.writestr(name, body)
    return output.getvalue()
