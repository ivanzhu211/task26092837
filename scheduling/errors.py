"""Exceptions raised by the scheduler."""

from __future__ import annotations


class SchedulingError(Exception):
    """Base class for scheduler errors."""


class NotFoundError(SchedulingError):
    """Referenced series or occurrence does not exist."""


class EditRejectedError(SchedulingError):
    """edit_from refused: an occurrence to be deleted has registrations."""


class RegistrationError(SchedulingError):
    """Registration refused (canceled occurrence or duplicate email)."""
