#!/usr/bin/env python3
"""Split a keymap-drawer SVG into true A4 pages for printing.

Why this exists: keymap-drawer emits one tall SVG. Printing that directly gives
one enormous page (or a shredded one, depending on your print dialog). This
splits it into real 210x297 mm A4 pages.

How it cuts: it groups panels into rows (the horizontal bands of the layout) and
only ever breaks *between* rows, so no layer panel or combo diagram is ever cut
in half. Rows are then packed top-to-bottom; if a row would overflow the page it
starts the next page.

Verification: the written page is re-parsed and every drawn element is checked to
land inside the printable window (10 mm margins, header strip reserved). Any
overflow is reported rather than silently shipped.

Usage:
    python3 scripts/a4_split.py                       # both diagrams -> docs/
    python3 scripts/a4_split.py --margin 12 --out docs
"""
import argparse
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET

A4_W, A4_H = 210.0, 297.0          # mm
MM_PER_PT = 25.4 / 72.0

# Print palette. keymap-drawer paints everything from its stylesheet: keys
# #f6f8fa, combo boxes #cdf, held keys #fdd. Those last two are luminance ~220
# and ~228 - a plain desaturate renders them as the SAME grey, silently losing
# the distinction between "this is a combo" and "this key is held". So the greys
# below are chosen for separation instead of computed by desaturation:
#   key white(255) / combo 204 / held 143  -> 51 and 61 levels apart.
GREY_PALETTE = {
    '#f6f8fa': '#ffffff',   # key fill      -> white (no toner for empty keycaps)
    '#cdf':    '#cccccc',   # combo box     -> light grey
    '#fdd':    '#8f8f8f',   # held key      -> dark grey
    '#c9cccf': '#808080',   # key/combo stroke (was very light, too faint to print)
    '#24292e': '#000000',   # text          -> pure black
    '#7b7e81': '#6b6b6b',   # transparent-key symbol
    '#57606a': '#444444',   # page subtitle
    '#d0d7de': '#b0b0b0',   # header rule
}
GREY_NAMES = {'gray': '#808080', 'grey': '#808080', 'silver': '#c0c0c0'}

# Colours we know keymap-drawer emits and must be mapped deliberately. Anything
# else falls back to black/white - fine for text, wrong for key fills, so the
# script reports it rather than printing something misleading.
EXPECTED_COLOURS = {'#f6f8fa', '#cdf', '#fdd', '#c9cccf'}

DEFAULT_JOBS = [
    # src svg,                      output pdf,                      header label
    ('urchin-keymap.svg',          'urchin-keymap-a4.pdf',          'Urchin keymap'),
    ('artsey-layer-reference.svg', 'artsey-layer-reference-a4.pdf', 'ARTSEY reference'),
]


def strip_ns(tag):
    return tag.split('}')[-1]


HEX3 = re.compile(r'#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])\b')
HEX6 = re.compile(r'#([0-9a-fA-F]{2})([0-9a-fA-F]{2})([0-9a-fA-F]{2})\b')


def _expand_hex(m):
    r, g, b = (m.group(i) * 2 for i in (1, 2, 3))
    return '#' + r + g + b


def _norm(hexv: str) -> str:
    """Normalise #abc -> #aabbcc so palette lookups can't silently miss."""
    return HEX3.sub(_expand_hex, hexv.lower())


# Palette keys arrive in both 3- and 6-digit forms in the wild (#cdf vs #ccddff).
# Normalise up front: matching 3-digit keys against already-expanded CSS is a
# silent failure mode that collapses combo/held onto the same grey.
GREY_PALETTE = {_norm(k): v for k, v in GREY_PALETTE.items()}


def greyscale_css(css: str) -> str:
    """Map every colour token in a stylesheet onto the print palette.

    Works on the stylesheet text rather than markup because keymap-drawer paints
    keys from CSS classes (rect.key / rect.combo / rect.held), and class rules
    cannot be overridden by inline attributes on the elements themselves.
    """
    css = HEX3.sub(_expand_hex, css)
    used = set()

    def repl(m):
        full = m.group(0).lower()
        if full in GREY_PALETTE:
            used.add(full)
            return GREY_PALETTE[full]
        r, g, b = (int(m.group(i), 16) for i in (1, 2, 3))
        # Unknown colour: Rec.601 luma snapped hard to black/white so nothing
        # prints as unreadable mid-grey text.
        y = (0.299 * r + 0.587 * g + 0.114 * b) / 255.0
        used.add(full + '?fallback')
        return '#000000' if y < 0.5 else '#ffffff'

    css = HEX6.sub(repl, css)
    # CSS keyword colours (path.combo uses `stroke: gray`, labels use `white`).
    for name, hexv in GREY_NAMES.items():
        if re.search(r'(?:fill|stroke|stop-color)\s*:\s*' + name + r'\b', css):
            used.add(name)
            css = re.sub(r'((?:fill|stroke|stop-color)\s*:\s*)' + name + r'\b',
                         lambda m, h=hexv: m.group(1) + h, css)
    _GREY_AUDIT.append(sorted(used))
    return css


_GREY_AUDIT = []


def translate_attr(tr):
    if not tr:
        return 0.0, 0.0
    m = re.search(r'translate\(\s*(-?[\d.]+)[ ,]+(-?[\d.]+)\s*\)', tr)
    if m:
        return float(m.group(1)), float(m.group(2))
    m = re.search(r'translate\(\s*(-?[\d.]+)\s*\)', tr)
    if m:
        return float(m.group(1)), 0.0
    return 0.0, 0.0


def extent(el):
    """Absolute (xlo, xhi, ylo, yhi) of a subtree, in SVG user units."""
    xlo, xhi, ylo, yhi = 1e9, -1e9, 1e9, -1e9
    stack = [(el, 0.0, 0.0)]
    while stack:
        node, ox, oy = stack.pop()
        tx, ty = translate_attr(node.get('transform'))
        ox, oy = ox + tx, oy + ty
        tag = strip_ns(node.tag)
        try:
            if tag == 'rect':
                x, y = float(node.get('x', 0)), float(node.get('y', 0))
                w, h = float(node.get('width', 0)), float(node.get('height', 0))
                xlo, xhi = min(xlo, ox + x), max(xhi, ox + x + w)
                ylo, yhi = min(ylo, oy + y), max(yhi, oy + y + h)
            elif tag == 'text':
                x, y = float(node.get('x', 0)), float(node.get('y', 0))
                xlo, xhi = min(xlo, ox + x - 22), max(xhi, ox + x + 22)
                ylo, yhi = min(ylo, oy + y - 10), max(yhi, oy + y + 10)
            elif tag == 'line' and node.get('x1') is not None:
                x1, y1 = float(node.get('x1', 0)), float(node.get('y1', 0))
                x2, y2 = float(node.get('x2', 0)), float(node.get('y2', 0))
                xlo, xhi = min(xlo, ox + min(x1, x2)), max(xhi, ox + max(x1, x2))
                ylo, yhi = min(ylo, oy + min(y1, y2)), max(yhi, oy + max(y1, y2))
        except (ValueError, TypeError):
            pass
        for child in node:
            stack.append((child, ox, oy))
    if xhi < xlo:
        return 0.0, 0.0, 0.0, 0.0
    return xlo, xhi, ylo, yhi


def analyse(path):
    raw = open(path).read()
    root = ET.fromstring(raw)
    head = raw[:raw.index('>') + 1]
    m = re.search(r'viewBox="([\d.\- ,]+)"', head)
    if not m:
        raise SystemExit(f'{path}: no viewBox in root <svg>')
    vb = re.split(r'[\s,]+', m.group(1).strip())
    panels = []
    for child in root:
        cls = child.get('class') or ''
        if strip_ns(child.tag) == 'style' or not cls.startswith('layer-'):
            continue
        xlo, xhi, ylo, yhi = extent(child)
        title = ''
        for t in child.iter():
            if strip_ns(t.tag) == 'text' and 'label' in (t.get('class') or ''):
                title = ''.join(t.itertext()).strip()
                break
        m = re.match(r'layer-combopos-(\d+)$', cls)
        panels.append(dict(el=child, cls=cls, top=ylo, bot=yhi, xlo=xlo, xhi=xhi,
                           title=title, idx=int(m.group(1)) if m else None))
    style = next((e for e in root if strip_ns(e.tag) == 'style'), None)
    return head, float(vb[2]), float(vb[3]), panels, style


def bands_of(panels):
    """Group panels into horizontal rows (vertical overlap => same row)."""
    bands, cur = [], None
    for p in sorted(panels, key=lambda x: (x['top'], x['bot'])):
        if cur and p['top'] < cur['bot'] - 1:
            cur['items'].append(p)
            cur['bot'] = max(cur['bot'], p['bot'])
            cur['top'] = min(cur['top'], p['top'])
            cur['xlo'] = min(cur['xlo'], p['xlo'])
            cur['xhi'] = max(cur['xhi'], p['xhi'])
        else:
            if cur:
                bands.append(cur)
            cur = dict(top=p['top'], bot=p['bot'], xlo=p['xlo'], xhi=p['xhi'], items=[p])
    if cur:
        bands.append(cur)
    return sorted(bands, key=lambda b: b['top'])


def paginate(bands, max_h, gap):
    """Greedy fill, then rebalance so the last page isn't nearly empty."""
    pages, cur = [], [0]
    for i in range(1, len(bands)):
        span = (bands[i]['bot'] + gap) - (bands[cur[0]]['top'] - gap)
        if span > max_h:
            pages.append(cur)
            cur = [i]
        else:
            cur.append(i)
    pages.append(cur)
    for _ in range(40):
        moved = False
        for k in range(len(pages) - 1):
            a, b = pages[k], pages[k + 1]
            while len(a) > 1:
                ha = (bands[a[-1]]['bot'] + gap) - (bands[a[0]]['top'] - gap)
                hb = (bands[b[-1]]['bot'] + gap) - (bands[b[0]]['top'] - gap)
                mv = (bands[a[-1]]['bot'] - bands[a[-1]]['top']) + gap
                if abs((ha - mv) - (hb + mv)) < abs(ha - hb) and (hb + mv) <= max_h:
                    b.insert(0, a.pop())
                    moved = True
                else:
                    break
        if not moved:
            break
    return pages


def build(src, out_pdf, label, margin_mm, hdr_mm, tmp_pref, gap=14.0, grey=True):
    head, _svg_w, _svg_h, panels, style = analyse(src)
    if not panels:
        raise SystemExit(f'{src}: no layer panels found - is this a keymap-drawer SVG?')
    bands = bands_of(panels)

    content_w = max(b['xhi'] for b in bands) - min(b['xlo'] for b in bands)
    scale = (A4_W - 2 * margin_mm) / content_w        # mm per user unit
    margin_u, hdr_u = margin_mm / scale, hdr_mm / scale
    vb_w, vb_h = A4_W / scale, A4_H / scale
    max_h = vb_h - 2 * margin_u - hdr_u
    dx = margin_u - min(b['xlo'] for b in bands)
    pages = paginate(bands, max_h, gap)
    fs = lambda pt: (pt * MM_PER_PT) / scale

    ET.register_namespace('', 'http://www.w3.org/2000/svg')
    ET.register_namespace('xlink', 'http://www.w3.org/1999/xlink')
    pdfs, problems = [], []

    for pi, idxs in enumerate(pages, 1):
        members = [p for i in idxs for p in bands[i]['items']]
        dy = (margin_u + hdr_u) - (bands[idxs[0]]['top'] - gap)

        titles = []
        for p in members:
            if p['title'] and p['title'] not in titles:
                titles.append(p['title'])
        ncombo = sum(1 for p in members if p['idx'] is not None)
        sub = '   '.join(titles) if titles else f'{ncombo} combo diagrams'
        hdr = f'{label} \u2014 page {pi} of {len(pages)}'

        nh = re.sub(r'width="[^"]*" height="[^"]*"',
                    f'width="{A4_W}mm" height="{A4_H}mm"', head, count=1)
        nh = re.sub(r'viewBox="[^"]*"', f'viewBox="0 0 {vb_w:.2f} {vb_h:.2f}"', nh, count=1)
        nh = nh[:nh.index('>')] + '>'

        parts = [nh]
        if style is not None:
            s = ET.tostring(style, encoding='unicode')
            s = s if s.startswith('<style') else re.sub(r'^<[\w:]+', '<style', s, 1)
            parts.append(greyscale_css(s) if grey else s)
        # The keymap stylesheet sets `text { text-anchor: middle }` and CSS beats
        # presentation attributes, so anchoring has to live in inline style.
        anchor = 'text-anchor:start;dominant-baseline:alphabetic'
        printable_u = vb_w - 2 * margin_u
        max_chars = max(20, int(printable_u / (0.62 * fs(8.5))))
        if len(sub) > max_chars:
            sub = sub[:max_chars - 1].rstrip(' ,\u2026') + '\u2026'
        hy = margin_u
        c_title = GREY_PALETTE['#24292e'] if grey else '#24292e'
        c_sub = GREY_PALETTE['#57606a'] if grey else '#57606a'
        c_rule = GREY_PALETTE['#d0d7de'] if grey else '#d0d7de'
        parts.append('<g class="page-header">')
        parts.append(f'<text x="{margin_u:.2f}" y="{hy + hdr_u*0.36:.2f}" '
                     f'style="{anchor};font-size:{fs(12):.1f}px;font-weight:bold;fill:{c_title}">{hdr}</text>')
        parts.append(f'<text x="{margin_u:.2f}" y="{hy + hdr_u*0.76:.2f}" '
                     f'style="{anchor};font-size:{fs(8.5):.1f}px;fill:{c_sub}">{sub}</text>')
        parts.append(f'<line x1="{margin_u:.2f}" y1="{hy + hdr_u*0.94:.2f}" '
                     f'x2="{margin_u + printable_u:.2f}" y2="{hy + hdr_u*0.94:.2f}" '
                     f'stroke="{c_rule}" stroke-width="1"/></g>')
        parts.append(f'<g class="page-body" transform="translate({dx:.2f},{dy:.2f})">')
        for p in members:
            parts.append(ET.tostring(p['el'], encoding='unicode'))
        parts.append('</g></svg>')

        page_svg = f'{tmp_pref}-p{pi}.svg'
        open(page_svg, 'w').write('\n'.join(parts))

        r2 = ET.fromstring('\n'.join(parts))
        xlo, xhi, ylo, yhi = 1e9, -1e9, 1e9, -1e9
        for e in r2:
            if (e.get('class') or '') == 'page-header':
                continue
            a, b, c, d = extent(e)
            if b > a or d > c:
                xlo, xhi = min(xlo, a), max(xhi, b)
                ylo, yhi = min(ylo, c), max(yhi, d)
        probs = []
        if ylo < hy + hdr_u * 0.94 - 1:
            probs.append(f'body overlaps header (y={ylo:.0f})')
        if yhi > vb_h - margin_u + 1:
            probs.append(f'bottom over by {(yhi - (vb_h - margin_u)) * scale:.1f}mm')
        if xlo < margin_u - 1:
            probs.append(f'left over by {(margin_u - xlo) * scale:.1f}mm')
        if xhi > vb_w - margin_u + 1:
            probs.append(f'right over by {(xhi - (vb_w - margin_u)) * scale:.1f}mm')
        if probs:
            problems.append(f'p{pi}: {probs}')

        page_pdf = f'{tmp_pref}-p{pi}.pdf'
        subprocess.run(['rsvg-convert', '-f', 'pdf', '-o', page_pdf, page_svg], check=True)
        pdfs.append(page_pdf)
        print(f'  page {pi}/{len(pages)}: {len(idxs)} rows, {len(members)} panels, '
              f'content {ylo*scale:.0f}-{yhi*scale:.0f}mm tall'
              + (f'   !! {probs}' if probs else ''))

    subprocess.run(['pdfunite', *pdfs, out_pdf], check=True)

    seen = sorted(p['cls'] for pg in pages for i in pg for p in bands[i]['items'])
    if seen != sorted(p['cls'] for p in panels):
        problems.append('panels not placed exactly once - split lost content')

    # Ground truth: rasterise the PDF we actually wrote and measure its pixels.
    # "I set grey fill colours" is not proof that a colour PDF did not slip out.
    if grey:
        problems.extend(check_palette_mapped())
        problems.extend(check_greyscale(out_pdf, tmp_pref))

    print(f'  -> {out_pdf}  ({len(pages)} pages)  '
          f'{"OK" if not problems else "PROBLEMS: " + str(problems)}')
    return len(pages), problems


def check_palette_mapped():
    """Every colour we expect to paint a key must have hit the palette, not the
    black/white fallback. Catches palette-key mismatches (e.g. 3-digit hex)."""
    seen = set()
    for entry in _GREY_AUDIT:
        seen |= set(entry)
    missed = {c for c in EXPECTED_COLOURS
              if _norm(c) not in seen and c + '?fallback' not in seen}
    fellback = sorted(c for c in seen if c.endswith('?fallback'))
    probs = []
    if missed:
        probs.append(f'palette never matched {sorted(missed)} (key fills may print wrong)')
    if fellback:
        probs.append(f'unknown colours hit the b/w fallback: {fellback}')
    return probs


def check_greyscale(pdf, tmp_pref):
    """Rasterise the finished PDF and prove every pixel is neutral."""
    problems = []
    try:
        from PIL import Image
        import numpy as np
    except ImportError:
        return ['greyscale check skipped (PIL/numpy unavailable)']

    dpi = 150
    base = f'{tmp_pref}-grey'
    tmpdir = os.path.dirname(base) or '.'
    subprocess.run(['pdftoppm', '-r', str(dpi), '-png', pdf, base], check=True)
    tiles = sorted(f for f in os.listdir(tmpdir)
                   if f.startswith(os.path.basename(base)) and f.endswith('.png'))
    if not tiles:
        return ['greyscale check found no rendered pages']
    for t in tiles:
        a = np.asarray(Image.open(os.path.join(tmpdir, t)).convert('RGB')).astype(int)
        sat = a.max(axis=2) - a.min(axis=2)          # 0 == perfectly neutral
        worst, nbad = int(sat.max()), int((sat > 6).sum())
        if nbad:
            problems.append(f'{t}: {nbad} non-grey pixels (max channel spread {worst})')
    if not problems:
        print(f'  greyscale: verified across {len(tiles)} rasterised pages')
    return problems


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--out', default='docs', help='output directory (default: docs)')
    ap.add_argument('--margin', type=float, default=10.0, help='page margin in mm (default 10)')
    ap.add_argument('--header', type=float, default=16.0, help='header strip height in mm')
    ap.add_argument('--tmp', default='/tmp/a4split', help='prefix for intermediate files')
    ap.add_argument('--only', help='only build this svg filename')
    ap.add_argument('--colour', '--color', dest='grey', action='store_false',
                    help='keep the original colours instead of the print palette')
    args = ap.parse_args()

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.chdir(root)
    os.makedirs(args.out, exist_ok=True)

    all_problems = {}
    for src, pdf_name, label in DEFAULT_JOBS:
        if args.only and src != args.only:
            continue
        if not os.path.exists(src):
            print(f'== {src} == skipped (not found)')
            continue
        print(f'== {src} ==')
        _n, probs = build(src, os.path.join(args.out, pdf_name), label,
                          args.margin, args.header, args.tmp, grey=args.grey)
        if probs:
            all_problems[src] = probs
    return 1 if all_problems else 0


if __name__ == '__main__':
    sys.exit(main())
