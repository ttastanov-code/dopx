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
        raise GitHubError(_explain(resp.status_code, resp.text))
    return resp.json() if resp.content else {}


def _explain(code: int, body: str) -> str:
    """Ошибка GitHub — понятным текстом для сообщения в боте."""
    if code == 401:
        return "GitHub не принял токен: он истёк или неверный (ADMIN_BOT_GITHUB_TOKEN)."
    if code == 403:
        return "У токена нет прав: нужны Actions — Read and write и Contents — Read."
    if code == 404:
        return "Не найден репозиторий или deploy.yml в main (проверьте ADMIN_BOT_GITHUB_REPO и доступ токена)."
    if code == 422 and "Unexpected inputs" in body:
        return "UNEXPECTED_INPUTS"
    return f"GitHub ответил {code}: {body[:150]}"


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
    try:
        _req("POST", f"/actions/workflows/{WORKFLOW}/dispatches", json={"ref": "main", "inputs": {"ref": ref}})
    except GitHubError as e:
        if str(e) != "UNEXPECTED_INPUTS":
            raise
        # В main старый deploy.yml без параметра ref: обычный деплой запускаем без него, откат — нельзя.
        if ref:
            raise GitHubError("Откат пока недоступен: в main старая версия deploy.yml. Слейте dev в main — и кнопка заработает.") from None
        _req("POST", f"/actions/workflows/{WORKFLOW}/dispatches", json={"ref": "main"})
