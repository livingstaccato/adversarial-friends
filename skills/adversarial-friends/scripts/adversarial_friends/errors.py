"""Exceptions that carry the process exit code they should produce."""


class AfError(Exception):
    exit_code = 1

    def __init__(self, message: str, exit_code: int | None = None) -> None:
        super().__init__(message)
        if exit_code is not None:
            self.exit_code = exit_code


class UsageError(AfError):
    exit_code = 2


class NoFriendsError(AfError):
    exit_code = 3


class CeilingError(AfError):
    exit_code = 11
