"""End-to-end test of the real GDI print path: renders a hand-written
2-page PDF (one portrait page, one landscape page, each with a black
rectangle) and prints it to "Microsoft Print to PDF" with `StartDoc`'s
`Output` pointed at a temp file, then asserts the spooled result is a
genuine 2-page, non-trivial PDF.

Skipped off Windows, and when "Microsoft Print to PDF" is not installed
(it ships with Windows 10/11, but a stripped-down CI image might lack it).
"""

import re
import sys

import pytest

TARGET_PRINTER = "Microsoft Print to PDF"

_TWO_PAGE_PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R 5 0 R]/Count 2>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 500]/Contents 4 0 R/Resources<<>>>>endobj
4 0 obj<</Length 39>>
stream
0 0 0 rg 20 20 100 100 re f
endstream
endobj
5 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 500 300]/Contents 6 0 R/Resources<<>>>>endobj
6 0 obj<</Length 39>>
stream
0 0 0 rg 20 20 100 100 re f
endstream
endobj
trailer<</Root 1 0 R>>
%%EOF
"""

_PAGE_RE = re.compile(rb"/Type\s*/Page(?!s)")


def _printer_available() -> bool:
    try:
        import win32print
    except ImportError:
        return False
    try:
        flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
        names = [p[2] for p in win32print.EnumPrinters(flags)]
    except Exception:
        return False
    return TARGET_PRINTER in names


@pytest.mark.skipif(not sys.platform.startswith("win"), reason="Windows-only print path")
@pytest.mark.skipif(not _printer_available(), reason=f"{TARGET_PRINTER!r} is not installed")
def test_print_two_page_pdf_to_microsoft_print_to_pdf(tmp_path):
    from tbhprint.backends.windows import _print_pdf

    src_pdf = tmp_path / "in.pdf"
    src_pdf.write_bytes(_TWO_PAGE_PDF)
    out_pdf = tmp_path / "out.pdf"

    job_id = _print_pdf(TARGET_PRINTER, str(src_pdf), copies=1, options=None,
                        title="tbhprint integration test", output_path=str(out_pdf))

    assert job_id.startswith(f"{TARGET_PRINTER}#")
    assert out_pdf.exists()
    data = out_pdf.read_bytes()

    # Non-trivial: a blank/near-empty PDF from a failed render would be tiny;
    # two rasterised pages at printer DPI comfortably clears this.
    assert len(data) > 20_000

    assert data[:5] == b"%PDF-"
    page_count = len(_PAGE_RE.findall(data))
    assert page_count == 2
