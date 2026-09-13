#!/usr/bin/env python3
"""
Prepare a (possibly blurry / scanned) schedule PDF for reading.

For each page it:
  1. Tries to pull the embedded text layer (fast path, exact).
  2. Renders the page at high DPI.
  3. Detects whether the page is a scan (image-only) or vector text.
  4. Writes an enhanced PNG: upscale -> grayscale -> CLAHE contrast ->
     denoise -> unsharp mask -> optional binarization.
  5. Also writes a "table" crop helper: splits wide pages into left/right
     halves so column text stays legible when zoomed.

Usage:
    python3 tools/prepare_schedule_pdf.py <pdf> [--out DIR] [--dpi 400]
"""

from __future__ import annotations

import argparse
import pathlib
import sys

import cv2
import numpy as np
import pymupdf


# ---------------------------------------------------------------- rendering
def render_page(page: "pymupdf.Page", dpi: int, gray: bool = False) -> np.ndarray:
    """Render a PDF page to a BGR (or gray) numpy array."""
    pix = page.get_pixmap(dpi=dpi, colorspace=pymupdf.csGRAY if gray else pymupdf.csRGB)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 1:
        return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)
    return cv2.cvtColor(img, cv2.COLOR_RGB2BGR)


def page_is_scan(page: "pymupdf.Page") -> bool:
    """Heuristic: a page with almost no extractable text is a scan."""
    text = page.get_text("text").strip()
    return len(text) < 40


# -------------------------------------------------------------- enhancement
def deskew(gray: np.ndarray) -> np.ndarray:
    """Rotate a page so its text baselines are horizontal."""
    inv = cv2.bitwise_not(gray)
    thr = cv2.threshold(inv, 0, 255, cv2.THRESH_BINARY | cv2.THRESH_OTSU)[1]
    coords = cv2.findNonZero(thr)
    if coords is None or len(coords) < 100:
        return gray
    angle = cv2.minAreaRect(coords)[-1]
    if angle < -45:
        angle = 90 + angle
    if abs(angle) < 0.3 or abs(angle) > 20:
        return gray
    h, w = gray.shape
    m = cv2.getRotationMatrix2D((w // 2, h // 2), angle, 1.0)
    return cv2.warpAffine(
        gray, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
    )


def enhance(img: np.ndarray, scale: float = 2.0, binarize: bool = False) -> np.ndarray:
    """Upscale + sharpen a rendered page so blurry text becomes readable."""
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    # remove scanner shading / uneven lighting before anything else
    bg = cv2.medianBlur(gray, 31)
    flat = cv2.divide(gray, bg, scale=255)

    flat = deskew(flat)

    if scale != 1.0:
        flat = cv2.resize(
            flat, None, fx=scale, fy=scale, interpolation=cv2.INTER_LANCZOS4
        )

    # local contrast (helps faded laser print)
    clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))
    flat = clahe.apply(flat)

    # unsharp mask - the actual "de-blur" step
    blur = cv2.GaussianBlur(flat, (0, 0), 3.0)
    sharp = cv2.addWeighted(flat, 1.7, blur, -0.7, 0)

    # a light bilateral pass kills jpeg/scan speckle without eating thin strokes
    sharp = cv2.bilateralFilter(sharp, 5, 40, 40)

    if binarize:
        sharp = cv2.adaptiveThreshold(
            sharp, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 12
        )

    return sharp


def split_halves(gray: np.ndarray, overlap: int = 60) -> tuple[np.ndarray, np.ndarray]:
    """Wide timetable pages: cut into left/right halves with a small overlap."""
    h, w = gray.shape
    mid = w // 2
    left = gray[:, max(0, 0) : min(w, mid + overlap)]
    right = gray[:, max(0, mid - overlap) : w]
    return left, right


# ---------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out", default=None, help="output dir (default: <pdf>_pages)")
    ap.add_argument("--dpi", type=int, default=400)
    ap.add_argument("--scale", type=float, default=2.0)
    ap.add_argument("--binarize", action="store_true")
    ap.add_argument("--no-split", action="store_true")
    args = ap.parse_args()

    pdf_path = pathlib.Path(args.pdf)
    if not pdf_path.exists():
        print(f"!! not found: {pdf_path}", file=sys.stderr)
        return 1

    out_dir = pathlib.Path(args.out) if args.out else pdf_path.with_suffix("").with_name(
        pdf_path.stem + "_pages"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = pymupdf.open(pdf_path)
    print(f"# {pdf_path.name}")
    print(f"pages={doc.page_count}  out={out_dir}")

    for i, page in enumerate(doc, start=1):
        raw_text = page.get_text("text").strip()
        scan = page_is_scan(page)
        kind = "SCAN/JPG" if scan else "TEXT-LAYER"
        print(f"\n--- page {i} [{kind}] text_chars={len(raw_text)}")

        if raw_text:
            (out_dir / f"p{i:02d}.txt").write_text(raw_text, encoding="utf-8")
            preview = "\n".join(raw_text.splitlines()[:15])
            print(preview)

        img = render_page(page, args.dpi)
        enhanced = enhance(img, scale=args.scale, binarize=args.binarize)
        cv2.imwrite(str(out_dir / f"p{i:02d}_enhanced.png"), enhanced)
        cv2.imwrite(str(out_dir / f"p{i:02d}_raw.png"), cv2.cvtColor(img, cv2.COLOR_BGR2GRAY))

        if not args.no_split:
            lft, rgt = split_halves(enhanced)
            cv2.imwrite(str(out_dir / f"p{i:02d}_L.png"), lft)
            cv2.imwrite(str(out_dir / f"p{i:02d}_R.png"), rgt)

        print(f"    wrote p{i:02d}_enhanced.png ({enhanced.shape[1]}x{enhanced.shape[0]})")

    doc.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
