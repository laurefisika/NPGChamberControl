"""Small terminal formatting helpers used by the command-line launcher."""

from __future__ import annotations

try:
    from colorama import Fore, Style, init as colorama_init
except ImportError:  # pragma: no cover - fallback for minimal environments
    class _Dummy:
        def __getattr__(self, _name: str) -> str:
            return ""

    Fore = Style = _Dummy()  # type: ignore

    def colorama_init(*_args, **_kwargs):
        return None


def init_colors() -> None:
    colorama_init()


def banner(message: str) -> None:
    line = "=" * max(10, len(message))
    print(f"\n{Fore.CYAN}{line}\n{message}\n{line}{Style.RESET_ALL}\n")
