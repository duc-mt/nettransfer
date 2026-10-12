"""Optional per-device credentials in the operating system's keyring (Windows Credential Manager, macOS Keychain,
Secret Service). Nothing is ever written to NetTransfer's own files, and nothing is used unless asked for.

Entries are stored as service ``nettransfer``, account ``<user>@<host>``; the host ``*`` is a fallback for every device.
"""

from __future__ import annotations

from typing import Any, Optional

SERVICE = "nettransfer"
DEFAULT_HOST = "*"


class KeyringUnavailableError(RuntimeError):
    pass


def _import_keyring() -> Any:
    try:
        import keyring
    except ImportError:
        raise KeyringUnavailableError("the 'keyring' package is not installed (pip install keyring)") from None
    return keyring


class CredentialStore:
    """``backend``: anything with get_password / set_password / delete_password (default: the ``keyring`` module)."""

    def __init__(self, backend: Any = None) -> None:
        self._backend = backend

    def _kr(self) -> Any:
        if self._backend is None:
            self._backend = _import_keyring()
        return self._backend

    @staticmethod
    def _account(host: str, username: str) -> str:
        return f"{username}@{host}"

    def get(self, host: str, username: str) -> Optional[str]:
        """Exact device first, then the ``*`` fallback; None if nothing is stored or the keyring is unusable."""
        try:
            kr = self._kr()
            for h in (host, DEFAULT_HOST):
                secret = kr.get_password(SERVICE, self._account(h, username))
                if secret:
                    return secret
        except Exception:  # missing package, locked/absent backend: fall back to prompting
            return None
        return None

    def set(self, host: str, username: str, password: str) -> None:
        if not username:
            raise ValueError("a username is needed to store a credential")
        try:
            self._kr().set_password(SERVICE, self._account(host, username), password)
        except KeyringUnavailableError:
            raise
        except Exception as exc:
            raise KeyringUnavailableError(f"the keyring refused the credential: {exc}") from None

    def delete(self, host: str, username: str) -> bool:
        try:
            self._kr().delete_password(SERVICE, self._account(host, username))
            return True
        except KeyringUnavailableError:
            raise
        except Exception:
            return False
