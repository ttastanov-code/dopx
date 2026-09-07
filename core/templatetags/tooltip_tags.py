# core/templatetags/tooltip_tags.py
"""
Тег {% tooltip_wrap "текст подсказки" %}...{% endtooltip_wrap %}.

Для случаев, когда подсказка должна всплывать при наведении/тапе на сам
элемент целиком (например бейдж-иконка без подписи рядом — вешать
отдельную info-иконку некуда и незачем, сам бейдж и есть триггер).

Для случаев "текст/лейбл + маленькая info-иконка рядом" используйте вместо
этого components/_tooltip_icon.html (см. комментарий в base.html о том,
почему подсказки больше не реализованы через CSS-класс `tooltip` +
`data-tip`).
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
        # Текст идёт в data-атрибут + HTML-escape, не аргументом x-data —
        # CSP-сборка Alpine не раскрывает \uXXXX внутри строковых литералов
        # аргументов. См. docs/adr/0019-alpine-csp-data-passing.md.
        safe_text = escape(text)
        # Видимость управляется вручную через display в :style (объектом),
        # а не через x-show/x-transition — на телепортированном (x-teleport)
        # узле эта связка оказалась ненадёжной: display молча "залипал" на
        # block даже когда open становился false (проверено вживую). Один
        # и тот же реактивный :style-объект для позиции и display работает
        # предсказуемо. См. подробный комментарий в _tooltip_icon.html.
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
