"""Render the IEUStudia icon from ``src/studia/assets/logo.svg`` into ``icon.png`` (256 px) and ``icon.ico``.

The SVG is the single source of the logo (the app's window, splash and sidebar use it too). It is
rasterised by headless Microsoft Edge through Playwright, so this is a development tool only:

    py -m pip install playwright      # once
    py scripts/make_icon.py
"""

from __future__ import annotations

import argparse
import struct
from pathlib import Path

ASSETS = Path(__file__).resolve().parents[1] / "src" / "studia" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)


def render_pngs(svg: str, sizes: tuple[int, ...]) -> dict[int, bytes]:
    from playwright.sync_api import sync_playwright

    out: dict[int, bytes] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge")
        page = browser.new_page(device_scale_factor=1)
        for size in sizes:
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(f'<html><body style="margin:0;background:transparent">'
                             f'{svg.replace("<svg ", f"<svg width={size} height={size} ", 1)}</body></html>')
            out[size] = page.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size})
        browser.close()
    return out


def ico_bytes(images: dict[int, bytes]) -> bytes:
    """An .ico file whose entries are PNG images (supported since Windows Vista)."""
    sizes = sorted(images)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries, blobs = b"", b""
    for s in sizes:
        data = images[s]
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    return header + entries + blobs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render the IEUStudia icon (PNG + ICO) from logo.svg.")
    parser.add_argument("--out", type=Path, default=ASSETS)
    args = parser.parse_args(argv)
    svg = (ASSETS / "logo.svg").read_text(encoding="utf-8")
    pngs = render_pngs(svg, SIZES)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "icon.png").write_bytes(pngs[256])
    (args.out / "icon.ico").write_bytes(ico_bytes(pngs))
    print(f"wrote {args.out / 'icon.png'} and {args.out / 'icon.ico'} ({', '.join(map(str, SIZES))} px)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
