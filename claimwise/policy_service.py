"""Mock carrier policy service. In production this would be a Guidewire / Duck Creek adapter."""
from __future__ import annotations
import json
from pathlib import Path
from urllib.parse import quote

from .normalize import norm_alnum


class PolicyService:
    """Looks up the carrier's own policy record (the source of truth for policy checks).

    Modes: in-memory dict (tests/eval), JSON file (default), or the mock REST API (mock_api/server.py).
    """

    def __init__(self, data: dict | None = None, path=None, api_url: str = ""):
        self.api_url = api_url.rstrip("/")
        if data is None and not self.api_url:
            p = Path(path) if path else None
            data = json.loads(p.read_text()) if p and p.exists() else {}
        self._data = {norm_alnum(k): v for k, v in (data or {}).items()}

    def get_policy(self, policy_no: str):
        if self.api_url:
            import requests
            r = requests.get(f"{self.api_url}/policies/{quote(policy_no)}", timeout=5)
            return None if r.status_code == 404 else r.json()
        return self._data.get(norm_alnum(policy_no))
