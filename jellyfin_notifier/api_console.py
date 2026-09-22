"""Exécution de requêtes API Jellyfin ad-hoc depuis la "console API" de
l'admin - réutilise le même schéma d'auth que jellyfin_client.py, renvoie le
JSON brut pour affichage direct dans le dashboard."""

from __future__ import annotations

import requests


def _auth_headers(api_key: str) -> dict:
    return {
        "Authorization": (
            'MediaBrowser Client="jellyfin-notifier-admin", Device="admin-console", '
            f'DeviceId="jellyfin-notifier-admin", Version="1.0.0", Token="{api_key}"'
        )
    }


def _parse_params(query_string: str) -> dict:
    """query_string : texte multi-lignes 'clé=valeur' (une paire par ligne)."""
    params = {}
    for line in (query_string or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        params[key.strip()] = value.strip()
    return params


def run_request(base_url: str, api_key: str, method: str, path: str, query_string: str) -> dict:
    params = _parse_params(query_string)
    url = base_url.rstrip("/") + "/" + path.lstrip("/")

    try:
        resp = requests.request(
            method.upper(),
            url,
            headers=_auth_headers(api_key),
            params=params,
            timeout=15,
        )
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        return {
            "status_code": resp.status_code,
            "ok": resp.ok,
            "url": resp.url,
            "body": body,
        }
    except requests.RequestException as exc:
        return {"status_code": None, "ok": False, "url": url, "body": str(exc)}
