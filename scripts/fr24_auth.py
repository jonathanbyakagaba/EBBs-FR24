"""Shared FR24 auth helper for the Entebbe-Flights scripts.

Resolution order for the subscription key:
  1. env var FR24_SUBSCRIPTION_KEY (GitHub Actions / Codespaces secret)
  2. fr24.conf next to it on Windows (%LOCALAPPDATA%\\fr24\\fr24\\fr24.conf)
  3. ~/.config/fr24/fr24.conf (Linux/Codespaces manual installs)

The key is passed directly to the fr24 library's login and is never
printed. Never commit or share fr24.conf or the key itself, and never
paste `fr24 auth show` output anywhere.
"""

from __future__ import annotations

import configparser
import os
import sys
from pathlib import Path


def read_subscription_key() -> str:
    env_key = os.environ.get("FR24_SUBSCRIPTION_KEY", "").strip()
    if env_key:
        return env_key
    candidates = []
    if os.environ.get("LOCALAPPDATA"):
        candidates.append(
            Path(os.environ["LOCALAPPDATA"]) / "fr24" / "fr24" / "fr24.conf")
    candidates.append(Path.home() / ".config" / "fr24" / "fr24.conf")
    for cfg_path in candidates:
        cfg = configparser.ConfigParser()
        if cfg.read(cfg_path):
            key = cfg.get("global", "subscription_key", fallback="").strip()
            if key:
                return key
    sys.exit("no FR24 subscription key: set FR24_SUBSCRIPTION_KEY or "
             f"fill subscription_key in one of: {candidates}")
