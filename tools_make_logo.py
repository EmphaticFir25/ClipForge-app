"""Generates the ClipForge logo files. Run: python3 tools_make_logo.py

The mark is a 16:9 frame (the full stream) with a 9:16 signal-red window (the clip),
cut out where the speaker is and breaking above the frame, so it reads as a reframe.
The wordmark is outlined from Inter Display ExtraBold so it renders without fonts.
"""
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

OUT = Path(__file__).parent / "webapp" / "static" / "brand"
FONT = "/usr/share/fonts/opentype/inter/InterDisplay-ExtraBold.otf"
SIGNAL = {"dark": "#ff4b2b", "light": "#e23a1c"}
INK = {"dark": "#efe9dc", "light": "#1b1915"}
GRAPHITE = "#121110"


def mark(frame: str, window: str, frame_w: float = 4.0) -> str:
    """64-unit mark. Frame spans x 4..60, y 17..48.5. Window is 18x32 at x 18, y 9."""
    return (
        f'<rect x="{4 + frame_w / 2}" y="{17 + frame_w / 2}" width="{56 - frame_w}" height="{31.5 - frame_w}" '
        f'fill="none" stroke="{frame}" stroke-width="{frame_w}"/>'
        f'<rect x="18" y="9" width="18" height="32" fill="{window}"/>'
    )


def wordmark_paths(text: str, cap_height: float, baseline: float, x0: float, color: str):
    font = TTFont(FONT)
    gs = font.getGlyphSet()
    cmap = font.getBestCmap()
    upm = font["head"].unitsPerEm
    s = cap_height / font["OS/2"].sCapHeight
    track = -0.012 * upm  # slightly tight: the display face sets well at this spacing
    cursor = 0.0
    parts = []
    for ch in text:
        name = cmap[ord(ch)]
        pen = SVGPathPen(gs)
        gs[name].draw(TransformPen(pen, (s, 0, 0, -s, x0 + cursor * s, baseline)))
        parts.append(pen.getCommands())
        cursor += gs[name].width + track
    width = (cursor - track) * s
    return f'<path d="{" ".join(parts)}" fill="{color}"/>', width


def write(name: str, body: str, vb: str):
    w, h = vb.split()[2:]
    svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{w}" height="{h}" role="img" aria-label="ClipForge">{body}</svg>\n'
    (OUT / name).write_text(svg, encoding="utf-8")


def build():
    OUT.mkdir(parents=True, exist_ok=True)
    for theme in ("dark", "light"):
        write(f"clipforge-mark-{theme}.svg", mark(INK[theme], SIGNAL[theme]), "0 0 64 64")
        text, width = wordmark_paths("ClipForge", cap_height=30, baseline=40, x0=74, color=INK[theme])
        total = 74 + width + 4
        write(f"clipforge-horizontal-{theme}.svg", mark(INK[theme], SIGNAL[theme]) + text, f"0 0 {total:.1f} 50")

    # Favicon: a graphite tile so it reads on any browser chrome. Heavier strokes for 16px.
    fav = (f'<rect width="32" height="32" rx="6" fill="{GRAPHITE}"/>'
           f'<rect x="2.5" y="8.5" width="27" height="15.5" fill="none" stroke="{INK["dark"]}" stroke-width="2.6"/>'
           f'<rect x="9.5" y="4" width="9" height="16" fill="{SIGNAL["dark"]}"/>')
    write("favicon.svg", fav, "0 0 32 32")
    print("logo files written to", OUT)


if __name__ == "__main__":
    build()
