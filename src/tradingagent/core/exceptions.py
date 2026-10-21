class TradingAgentError(Exception):
    """Base class for expected application errors."""


class UnsafeModeError(TradingAgentError):
    """Raised when a mode is requested before its safety implementation exists."""
