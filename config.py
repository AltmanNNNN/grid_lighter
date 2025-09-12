import json
from typing import Any


class Config:
    # Lighter SDK (optional fields until wired-in everywhere)
    api_host: str | None
    market_id_map: dict[str, int] | None
    account_index: int | None
    api_key_index: int | None
    api_key_private_key: str | None

    def __init__(self, **kwargs: Any) -> None:
        # Set all keys as attributes for simplicity
        for k, v in kwargs.items():
            setattr(self, k, v)

    @staticmethod
    def load(path: str = "config.json") -> "Config":
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        return Config(**raw)
