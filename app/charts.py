"""Server-rendered inline SVG trend chart (no JS chart library): daily clicks with change markers."""

from __future__ import annotations

from datetime import date, timedelta
from html import escape


def trend_svg(series: list[tuple[str, int]], markers: list[str], label: str, width: int = 720, height: int = 180) -> str:
    if not series:
        return '<p class="muted">No data for this period yet.</p>'
    start, end = date.fromisoformat(series[0][0]), date.fromisoformat(series[-1][0])
    span = max((end - start).days, 1)
    vals = dict(series)
    days = [(start + timedelta(days=i)).isoformat() for i in range(span + 1)]
    ys = [vals.get(d, 0) for d in days]
    top = max(max(ys), 1)
    pad_l, pad_r, pad_t, pad_b = 36, 8, 10, 22
    w, h = width - pad_l - pad_r, height - pad_t - pad_b

    def x(i: int) -> float:
        return pad_l + w * i / span

    def y(v: float) -> float:
        return pad_t + h - h * v / top

    pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(ys))
    area = f"{pad_l},{pad_t + h} {pts} {x(span):.1f},{pad_t + h}"
    mk = []
    for m in markers:
        if start.isoformat() <= m <= end.isoformat():
            i = (date.fromisoformat(m) - start).days
            mk.append(f'<line class="marker" x1="{x(i):.1f}" x2="{x(i):.1f}" y1="{pad_t}" y2="{pad_t + h}"><title>change {escape(m)}</title></line>')
    grid = "".join(
        f'<line class="grid" x1="{pad_l}" x2="{pad_l + w}" y1="{y(top * f):.1f}" y2="{y(top * f):.1f}"/>'
        f'<text class="axis" x="{pad_l - 6}" y="{y(top * f) + 4:.1f}" text-anchor="end">{round(top * f)}</text>'
        for f in (0, 0.5, 1))
    return (f'<svg class="trend" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">'
            f'{grid}<polygon class="area" points="{area}"/><polyline class="line" points="{pts}"/>{"".join(mk)}'
            f'<text class="axis" x="{pad_l}" y="{height - 6}">{start.isoformat()}</text>'
            f'<text class="axis" x="{pad_l + w}" y="{height - 6}" text-anchor="end">{end.isoformat()}</text></svg>')
