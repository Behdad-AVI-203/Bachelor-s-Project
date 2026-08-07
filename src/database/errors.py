"""Database-specific exception hierarchy."""


class DatabaseError(Exception):
    """Base exception for database-layer failures."""


class DatabaseIntegrityError(DatabaseError):
    """Raised when a database constraint is violated."""


class RecordNotFoundError(DatabaseError):
    """Raised when a requested database record does not exist."""


class ValidationError(DatabaseError):
    """Raised when invalid table names, fields, or values are supplied."""


class ConfigurationError(DatabaseError):
    """Raised when a configuration cannot be exported or imported."""
