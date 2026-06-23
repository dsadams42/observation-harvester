from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

DEFAULT_AGENT_MODEL = "gpt-5.4-mini"


class MissingAPIKeyError(RuntimeError):
    """Raised when agent mode is requested without an OpenAI API key."""


@dataclass(frozen=True)
class Settings:
    agent_model: str = DEFAULT_AGENT_MODEL
    openai_api_key: str | None = None

    def require_openai_api_key(self) -> None:
        if not self.openai_api_key:
            raise MissingAPIKeyError(
                "OPENAI_API_KEY is required for agent mode. "
                "Set it in the environment and retry."
            )


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    values = os.environ if env is None else env
    return Settings(
        agent_model=values.get("PDT_OBSERVER_MODEL", DEFAULT_AGENT_MODEL),
        openai_api_key=values.get("OPENAI_API_KEY"),
    )
