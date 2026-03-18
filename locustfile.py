# locustfile.py
from locust import HttpUser, task, between
import random

class DOPXUser(HttpUser):
    wait_time = between(1, 3)
    
    def on_start(self):
        # Логин
        self.client.post('/api/auth/login/', {
            'username': 'benchmark_user',
            'password': 'testpass123'
        })
    
    @task(3)
    def get_player_aggregates(self):
        self.client.get('/api/player-aggregate/')
    
    @task(3)
    def get_match_aggregates(self):
        self.client.get('/api/match-aggregate/')
    
    @task(2)
    def get_top_players(self):
        self.client.get('/api/player-aggregate/top_players/?limit=10')
    
    @task(1)
    def get_recent_matches(self):
        self.client.get('/api/match-aggregate/recent/?limit=10')