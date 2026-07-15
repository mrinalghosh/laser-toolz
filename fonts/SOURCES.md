# Bundled glyph-mode fonts

Glyph mode (`linify.py --mode glyph`) reads each glyph's native outline via
freetype, so the fonts live here as files. `python fetch_fonts.py` downloads
them (the three small ones are also committed; `unifont.otf` is gitignored).

All four are licensed under the **SIL Open Font License, Version 1.1**
(https://scripts.sil.org/OFL) — free to bundle and redistribute.

| File | Font | Role in the packs | Source |
|---|---|---|---|
| `NotoSansSymbols2-Regular.ttf` | Noto Sans Symbols 2 (Google) | symbol/arrow fallback tail | github.com/google/fonts `ofl/notosanssymbols2` |
| `JetBrainsMono-Regular.ttf` | JetBrains Mono | `mono` pack primary | github.com/JetBrains/JetBrainsMono |
| `Spectral-Regular.ttf` | Spectral (Production Type / Google) | `serif` pack primary | github.com/google/fonts `ofl/spectral` |
| `unifont.otf` | GNU Unifont 15.1.05 | `unifont` pack — near-total BMP coverage | unifoundry.com/unifont |

The `system`/`sans` packs and the fallback tails also reference macOS system
fonts by path (Apple Symbols, STIX Two Math, Hiragino Sans GB, Helvetica, …);
those are **not** redistributed — they are read from `/System/Library/Fonts`.
On non-macOS hosts those paths are skipped and the bundled fonts still work.
