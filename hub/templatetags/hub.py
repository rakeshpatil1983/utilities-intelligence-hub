from django import template

register = template.Library()


def _float(v):
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


@register.filter
def pct(v, digits=0):
    v = _float(v)
    return "—" if v is None else f"{v * 100:.{int(digits)}f} %"


@register.filter
def num(v, digits=0):
    v = _float(v)
    return "—" if v is None else f"{v:,.{int(digits)}f}"


@register.filter
def iso(v):
    return v.isoformat(timespec="minutes") if v else ""


@register.filter
def health_class(v):
    return "good" if v >= 80 else "warn" if v >= 50 else "bad"


@register.filter
def get(d, k):
    return d.get(k) if d else None


RING_C = 2 * 3.14159 * 30


@register.filter
def ring(v):
    """SVG dash length for a 0–100 score on an r=30 ring."""
    v = _float(v) or 0
    return f"{max(0, min(100, v)) / 100 * RING_C:.1f} {RING_C:.1f}"


@register.filter
def status_of(v):
    v = _float(v)
    return "good" if v is not None and v >= 80 else "warning" if v is not None and v >= 50 else "critical"


@register.filter
def status_label(v):
    return {"good": "Healthy", "warning": "Needs attention", "critical": "Critical"}[status_of(v)]


@register.filter
def share(n, total):
    n, total = _float(n) or 0, _float(total) or 0
    return f"{n / total * 100:.2f}" if total else "0"
