# adminbot/github.py
"""Деплой и откат через workflow_dispatch deploy.yml.
Токен — fine-grained PAT на репозиторий: Actions RW, Contents R."""
from __future__ import annotations

import requests
from django.conf import settings

API = "https://api.github.com"
WORKFLOW = "deploy.yml"


class GitHubError(Exception):
    pass


def configured() -> bool:
    return bool(settings.ADMIN_BOT_GITHUB_TOKEN and settings.ADMIN_BOT_GITHUB_REPO)


def _req(method: str, path: str, **kw):
    resp = requests.request(method, f"{API}/repos/{settings.ADMIN_BOT_GITHUB_REPO}{path}", timeout=15, headers={
        "Authorization": f"Bearer {settings.ADMIN_BOT_GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
    }, **kw)
    if resp.status_code >= 400:
        raise GitHubError(f"{resp.status_code}: {resp.text[:200]}")
    return resp.json() if resp.content else {}


def releases(limit: int = 5) -> list[dict]:
    """Последние релизы: [{tag, name, published}], новые первыми."""
    return [{"tag": r["tag_name"], "published": r.get("published_at", "")[:10]}
            for r in _req("GET", f"/releases?per_page={limit}") if not r.get("draft")]


def runs(limit: int = 3) -> list[dict]:
    data = _req("GET", f"/actions/workflows/{WORKFLOW}/runs?per_page={limit}")
    return [{"status": r["status"], "conclusion": r.get("conclusion"), "url": r["html_url"],
             "title": r.get("display_title", ""), "created": r.get("created_at", "")[:16].replace("T", " ")}
            for r in data.get("workflow_runs", [])]


def dispatch(ref: str = "") -> None:
    """Запустить деплой: пусто — последний main (с тестами), тег vX.Y.Z — откат на эту версию."""
    _req("POST", f"/actions/workflows/{WORKFLOW}/dispatches", json={"ref": "main", "inputs": {"ref": ref}})
