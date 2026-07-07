"""Runtime configuration loaded from the environment or a ``.env`` file."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict

ZENODO_URL = "https://zenodo.org"
ZENODO_SANDBOX_URL = "https://sandbox.zenodo.org"


class Settings(BaseSettings):
    """API tokens for zenodo.org and sandbox.zenodo.org.

    Tokens are read from the environment variables ``ZENODO_TOKEN`` and
    ``ZENODO_SANDBOX_TOKEN`` (or a local ``.env`` file). Both need the
    ``deposit:write`` and ``deposit:actions`` scopes.

    Examples:
        >>> Settings(zenodo_token="t", _env_file=None).zenodo_token
        't'
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    zenodo_token: str | None = None
    zenodo_sandbox_token: str | None = None

    def token_for(self, sandbox: bool) -> str:
        """Return the token for the chosen instance or raise a helpful error.

        Examples:
            >>> Settings(zenodo_sandbox_token="s", _env_file=None).token_for(sandbox=True)
            's'
            >>> Settings(_env_file=None).token_for(sandbox=False)
            Traceback (most recent call last):
            ...
            ValueError: ZENODO_TOKEN is not set. Create a token with the scopes \
deposit:write and deposit:actions at https://zenodo.org/account/settings/applications/tokens/new/
        """
        name = "ZENODO_SANDBOX_TOKEN" if sandbox else "ZENODO_TOKEN"
        token = self.zenodo_sandbox_token if sandbox else self.zenodo_token
        if not token:
            base = ZENODO_SANDBOX_URL if sandbox else ZENODO_URL
            raise ValueError(
                f"{name} is not set. Create a token with the scopes deposit:write "
                f"and deposit:actions at {base}/account/settings/applications/tokens/new/"
            )
        return token


def base_url_for(sandbox: bool) -> str:
    """Return the API base URL for the chosen Zenodo instance.

    Examples:
        >>> base_url_for(sandbox=False)
        'https://zenodo.org'
        >>> base_url_for(sandbox=True)
        'https://sandbox.zenodo.org'
    """
    return ZENODO_SANDBOX_URL if sandbox else ZENODO_URL
