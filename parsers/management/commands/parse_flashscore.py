from django.core.management.base import BaseCommand

from parsers.flashscore.pipeline import run


class Command(BaseCommand):

    help = "Parse Flashscore Kazakhstan Premier League"

    def handle(self, *args, **kwargs):

        self.stdout.write("Starting Flashscore parser")

        run()

        self.stdout.write(self.style.SUCCESS("Parsing finished"))