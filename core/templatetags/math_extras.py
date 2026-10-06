# core/templatetags/math_extras.py
from django import template

register = template.Library()

@register.filter
def get_item(dictionary, key):
    """Значение из словаря по ключу."""
    return dictionary.get(key) if dictionary else None

@register.filter
def div(value, arg):
    """value / arg"""
    try:
        return float(value) / float(arg)
    except (ValueError, ZeroDivisionError, TypeError):
        return 0

@register.filter
def mul(value, arg):
    """value * arg"""
    try:
        return float(value) * float(arg)
    except (ValueError, TypeError):
        return 0
    
    
@register.filter
def subtract(value, arg):
    """value - arg"""
    try:
        return float(value) - float(arg)
    except (ValueError, TypeError):
        return 0


@register.filter
def bipolar_bar_pct(value, scale=10):
    """Ширина бара для метрики в диапазоне [-scale, +scale] (0 — середина), с клампом 0..100."""
    try:
        value = float(value)
        scale = float(scale)
    except (ValueError, TypeError):
        return 0
    if scale <= 0:
        return 0
    pct = (value + scale) / (2 * scale) * 100
    return max(0, min(100, round(pct)))

@register.filter
def bipolar_half_pct(value, scale=10):
    """Длина полосы от центра для шкалы [-scale, +scale]: доля половины дорожки, 0..50 (%)."""
    try:
        value, scale = abs(float(value)), float(scale)
    except (ValueError, TypeError):
        return 0
    # Строкой с точкой: число в шаблоне локализуется в «7,1», а такую ширину CSS отбрасывает.
    return "0" if scale <= 0 else f"{min(value, scale) / scale * 50:.1f}"
