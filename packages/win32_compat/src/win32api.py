"""win32api subset for mcp (#181); see _robomate_win32."""

from _robomate_win32 import CloseHandle, OpenProcess

__all__ = ["CloseHandle", "OpenProcess"]
