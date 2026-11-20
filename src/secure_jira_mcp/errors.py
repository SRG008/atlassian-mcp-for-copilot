"""User-facing errors. Their messages are shown to the model and the user, so they never contain secrets."""


class SecureJiraError(Exception):
    """Base class."""


class ConfigError(SecureJiraError):
    """Invalid configuration or a host that is not allowed."""


class AuthError(SecureJiraError):
    """Missing or rejected credentials."""


class PolicyError(SecureJiraError):
    """The operation is disabled by the administrator's policy, read-only mode, or the rate limit."""


class ValidationError(SecureJiraError):
    """Invalid arguments."""


class JiraError(SecureJiraError):
    """Jira answered with an error."""
