# core/templatetags/tooltip_tags.py
"""{% tooltip_wrap "текст" %}...{% endtooltip_wrap %} — подсказка на весь элемент.
Для «лейбл + info-иконка» — components/_tooltip_icon.html.
"""
from django import template
from django.utils.html import escape

register = template.Library()


class TooltipWrapNode(template.Node):
    def __init__(self, text_var, nodelist):
        self.text_var = text_var
        self.nodelist = nodelist

    def render(self, context):
        text = self.text_var.resolve(context)
        inner = self.nodelist.render(context)
        if not text:
            return inner
        # Текст в data-атрибут с экранированием, не аргументом x-data.
        safe_text = escape(text)
        # Видимость через display в :style (x-show на телепортированном узле ненадёжен).
        return (
            f'<span class="relative inline-flex" x-data="tooltipTrigger" data-tooltip-text="{safe_text}" @click.outside="hide()">'
            f'<span tabindex="0" x-ref="trigger" class="cursor-help outline-none" '
            f'@mouseenter="show()" @mouseleave="hide()" @click.stop.prevent="show()" '
            f'@focus="show()" @blur="hide()">{inner}</span>'
            f'<template x-teleport="body">'
            f'<div x-ref="bubble" x-cloak '
            f':style="{{ position: \'fixed\', top: pos.top + \'px\', left: pos.left + \'px\', display: open ? \'block\' : \'none\' }}" '
            f'class="z-[9999] max-w-[240px] rounded-md bg-neutral text-neutral-content text-xs leading-snug '
            f'px-2.5 py-1.5 shadow-lg pointer-events-none"><span x-text="text"></span></div>'
            f'</template>'
            f'</span>'
        )


@register.tag(name="tooltip_wrap")
def tooltip_wrap(parser, token):
    bits = token.split_contents()
    if len(bits) != 2:
        raise template.TemplateSyntaxError(
            "'tooltip_wrap' требует один аргумент: {% tooltip_wrap \"текст\" %}...{% endtooltip_wrap %}"
        )
    text_var = parser.compile_filter(bits[1])
    nodelist = parser.parse(("endtooltip_wrap",))
    parser.delete_first_token()
    return TooltipWrapNode(text_var, nodelist)
