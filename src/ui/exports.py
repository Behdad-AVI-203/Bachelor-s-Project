"""CSV and dependency-free PDF export helpers."""

from __future__ import annotations

import io
import textwrap
from collections.abc import Iterable
from typing import Any

import pandas as pd


def dataframe_to_csv(dataframe: pd.DataFrame) -> bytes:
    """Return a UTF-8 CSV including a spreadsheet-friendly BOM."""
    return dataframe.to_csv(index=False).encode("utf-8-sig")


def results_pdf(
    *,
    title: str,
    simulation_id: int,
    rows: Iterable[dict[str, Any]],
) -> bytes:
    """Create a compact text PDF without adding another dependency."""
    lines = [
        title,
        f"Simulation ID: {simulation_id}",
        "",
        "Final network metrics",
        "",
    ]
    for row in rows:
        network = row.get("Network", "Network")
        lines.append(network)
        for key, value in row.items():
            if key != "Network":
                lines.append(f"  {key}: {value}")
        lines.append("")
    return _build_text_pdf(lines)


def _build_text_pdf(lines: list[str]) -> bytes:
    wrapped_lines = []
    for line in lines:
        wrapped_lines.extend(textwrap.wrap(str(line), width=92) or [""])

    pages = [
        wrapped_lines[index : index + 48]
        for index in range(0, len(wrapped_lines), 48)
    ] or [[]]
    objects: list[bytes] = []
    font_object_id = 3
    page_object_ids = []
    content_object_ids = []
    next_object_id = 4

    for _ in pages:
        page_object_ids.append(next_object_id)
        content_object_ids.append(next_object_id + 1)
        next_object_id += 2

    kids = " ".join(f"{object_id} 0 R" for object_id in page_object_ids)
    objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
    objects.append(
        (
            f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>"
        ).encode("ascii")
    )
    objects.append(
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"
    )

    for page_index, page_lines in enumerate(pages):
        content_id = content_object_ids[page_index]
        page = (
            "<< /Type /Page /Parent 2 0 R "
            "/MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_object_id} 0 R >> >> "
            f"/Contents {content_id} 0 R >>"
        ).encode("ascii")
        objects.append(page)

        commands = ["BT", "/F1 10 Tf", "48 790 Td", "14 TL"]
        for line in page_lines:
            commands.append(f"({_pdf_escape(line)}) Tj")
            commands.append("T*")
        commands.append("ET")
        stream = "\n".join(commands).encode("latin-1", errors="replace")
        objects.append(
            (
                f"<< /Length {len(stream)} >>\nstream\n".encode("ascii")
                + stream
                + b"\nendstream"
            )
        )

    output = io.BytesIO()
    output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for object_id, body in enumerate(objects, start=1):
        offsets.append(output.tell())
        output.write(f"{object_id} 0 obj\n".encode("ascii"))
        output.write(body)
        output.write(b"\nendobj\n")

    cross_reference = output.tell()
    output.write(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    output.write(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        output.write(f"{offset:010d} 00000 n \n".encode("ascii"))
    output.write(
        (
            f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
            f"startxref\n{cross_reference}\n%%EOF"
        ).encode("ascii")
    )
    return output.getvalue()


def _pdf_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
