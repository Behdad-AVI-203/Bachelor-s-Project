"""Core business-logic exception hierarchy."""


class CoreError(Exception):
    """Base exception for core-domain failures."""


class EnvironmentError(CoreError):
    """Raised when an IoT environment is invalid."""


class BlockchainError(CoreError):
    """Raised when a blockchain engine cannot process its state."""


class SimulationError(CoreError):
    """Raised when simulation orchestration fails."""


class PluginError(CoreError):
    """Base exception for user-provided Python plugin failures."""


class PluginValidationError(PluginError):
    """Raised when uploaded Python source violates the plugin contract."""


class PluginExecutionError(PluginError):
    """Raised when a validated plugin fails during execution."""
