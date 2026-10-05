# core/management/commands/audit_data.py
"""manage.py audit_data

Проверка целостности данных (core/integrity.py): счёт и события, таблица, голоса в рейтингах,
уровни и серии, составы, сборные. Только чтение; итог сохраняется для дашборда и алертов бота.
"""
from django.core.management.base import BaseCommand

from core.integrity import run_and_store


class Command(BaseCommand):
    help = "Проверка целостности данных (только чтение)."

    def handle(self, *args, **options):
        report = run_and_store()
        for f in report["findings"]:
            if not f["count"]:
                self.stdout.write(f"  ✓ {f['title']}")
                continue
            style = self.style.ERROR if f["severity"] == "error" else self.style.WARNING
            self.stdout.write(style(f"  ✗ {f['title']}: {f['count']}"))
            for sample in f["samples"]:
                self.stdout.write(f"      {sample}")
            if f["hint"]:
                self.stdout.write(f"      → {f['hint']}")
        summary = f"Ошибок: {report['errors']}, предупреждений: {report['warnings']}."
        self.stdout.write(self.style.ERROR(summary) if report["errors"] else self.style.SUCCESS(summary))
