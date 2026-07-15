#!/usr/bin/env python3
"""Download the bundled glyph-mode font packs into ``fonts/``.

The glyph mode reads each glyph's *native outline* via freetype, so the fonts
must exist as files on disk (unlike the glyph-archive web app, which streams
them from Google Fonts). The three small OFL faces are committed to the repo;
Unifont (~5 MB) is gitignored and fetched here, mirroring the ``weights/``
pattern. Re-running only downloads what is missing (pass --force to refetch).

All four are SIL Open Font License 1.1 — see fonts/SOURCES.md.
"""
import argparse
import os
import sys
import urllib.request

FONT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")

# name -> (filename, url). Kept in sync with linify._GLYPH_PACKS.
FONTS = {
    "Noto Sans Symbols 2": (
        "NotoSansSymbols2-Regular.ttf",
        "https://github.com/google/fonts/raw/main/ofl/notosanssymbols2/NotoSansSymbols2-Regular.ttf",
    ),
    "JetBrains Mono": (
        "JetBrainsMono-Regular.ttf",
        "https://github.com/JetBrains/JetBrainsMono/raw/master/fonts/ttf/JetBrainsMono-Regular.ttf",
    ),
    "Spectral": (
        "Spectral-Regular.ttf",
        "https://github.com/google/fonts/raw/main/ofl/spectral/Spectral-Regular.ttf",
    ),
    "GNU Unifont": (
        "unifont.otf",
        "https://unifoundry.com/pub/unifont/unifont-15.1.05/font-builds/unifont-15.1.05.otf",
    ),
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--force", action="store_true",
                    help="re-download even fonts already present")
    ns = ap.parse_args(argv)

    os.makedirs(FONT_DIR, exist_ok=True)
    for name, (fname, url) in FONTS.items():
        dest = os.path.join(FONT_DIR, fname)
        if os.path.exists(dest) and not ns.force:
            print(f"  have  {fname}")
            continue
        print(f"  get   {fname}  <- {url}")
        try:
            with urllib.request.urlopen(url) as r, open(dest, "wb") as fh:
                fh.write(r.read())
        except Exception as exc:                            # noqa: BLE001
            print(f"  FAIL  {name}: {exc}", file=sys.stderr)
            return 1
    print(f"fonts ready in {FONT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
