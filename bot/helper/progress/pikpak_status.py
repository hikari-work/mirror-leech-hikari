from .base import BaseStatus


class PikPakStatus(BaseStatus):
    """Status line for a PikPak task; the listing declares every file's size."""

    tool = "pikpak"
