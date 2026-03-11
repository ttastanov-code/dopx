import requests


BASE_URL = "https://www.flashscorekz.com"


class FlashscoreClient:

    def get(self, url):

        headers = {
            "User-Agent": "Mozilla/5.0"
        }

        response = requests.get(url, headers=headers)

        response.raise_for_status()

        return response.text