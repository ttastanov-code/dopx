# partners/forms.py
"""Форма баннера для дашборда: обязательные поля зависят от формата, пропорции картинки сверяются с зоной."""
from __future__ import annotations

from django import forms

from .models import Banner, BannerFormat, Partner, ZONE_SPECS

# Допустимое отклонение пропорций картинки от рамки зоны; больше — предупреждаем об обрезке.
RATIO_TOLERANCE = 0.08
MAX_IMAGE_BYTES = 2 * 1024 * 1024


class ImageInput(forms.ClearableFileInput):
    template_name = "partners/widgets/image_input.html"


class BannerForm(forms.ModelForm):
    class Meta:
        model = Banner
        fields = [
            "title", "partner", "advertiser", "zone", "format", "target_url",
            "image", "image_mobile", "logo", "headline", "body", "cta_label",
            "is_active", "starts_at", "ends_at", "priority",
            "audience", "max_impressions", "max_clicks", "daily_cap_per_visitor", "requires_age_disclaimer",
        ]
        widgets = {
            "starts_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "ends_at": forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"),
            "image": ImageInput, "image_mobile": ImageInput, "logo": ImageInput,
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["partner"].queryset = Partner.objects.filter(is_active=True).order_by("name")
        self.fields["partner"].empty_label = "Собственное промо DOPX"
        for name in ("starts_at", "ends_at"):
            self.fields[name].input_formats = ["%Y-%m-%dT%H:%M"]
        self.warnings: list[str] = []
        for name, field in self.fields.items():
            widget = field.widget
            if isinstance(widget, forms.CheckboxInput):
                widget.attrs["class"] = "checkbox checkbox-sm"
            elif isinstance(widget, forms.ClearableFileInput):
                widget.attrs.update({"class": "file-input file-input-sm file-input-bordered w-full", "accept": "image/*"})
            elif isinstance(widget, forms.Select):
                widget.attrs["class"] = "select select-sm select-bordered w-full"
            else:
                widget.attrs["class"] = "input input-sm input-bordered w-full"

    def _check_image(self, field: str, target: tuple, label: str):
        image = self.cleaned_data.get(field)
        if not image or not hasattr(image, "image"):  # файл не меняли
            return
        if image.size > MAX_IMAGE_BYTES:
            self.add_error(field, "Файл больше 2 МБ. Сожмите картинку, иначе страница будет грузиться медленно.")
            return
        w, h = image.image.size
        want = target[0] / target[1]
        if abs(w / h - want) / want > RATIO_TOLERANCE:
            self.warnings.append(
                f"{label}: {w}×{h}, а рамка зоны {target[0]}×{target[1]}. Края картинки обрежутся."
            )
        if w < target[0] * 0.75:
            self.warnings.append(f"{label}: ширина {w} px, на экранах высокой чёткости будет мыльно. Лучше {target[0]} px.")

    def clean(self):
        data = super().clean()
        fmt = data.get("format")
        zone = data.get("zone")
        if fmt == BannerFormat.NATIVE:
            if not data.get("headline"):
                self.add_error("headline", "Для карточки нужен заголовок.")
        elif not data.get("image") and not (self.instance.pk and self.instance.image):
            self.add_error("image", "Для формата «Картинка» загрузите картинку для компьютера.")
        starts, ends = data.get("starts_at"), data.get("ends_at")
        if starts and ends and ends <= starts:
            self.add_error("ends_at", "Окончание должно быть позже начала.")
        if zone in ZONE_SPECS and fmt == BannerFormat.IMAGE:
            spec = ZONE_SPECS[zone]
            self._check_image("image", spec["desktop"], "Картинка для компьютера")
            self._check_image("image_mobile", spec["mobile"], "Картинка для телефона")
        return data
