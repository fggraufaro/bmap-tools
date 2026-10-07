"""Extra secret detectors for detect-secrets, for key formats it does not know.

Used by the pre-commit hook (see .pre-commit-config.yaml). These match the
shape of a key, never a real value; there are no secrets in this file.
"""
import re

from detect_secrets.plugins.base import RegexBasedDetector


class SupabaseSecretKeyDetector(RegexBasedDetector):
    secret_type = "Supabase secret key"  # pragma: allowlist secret
    denylist = [re.compile(r"sb_secret_[A-Za-z0-9_\-]{20,}")]


class AnthropicKeyDetector(RegexBasedDetector):
    secret_type = "Anthropic API key"  # pragma: allowlist secret
    denylist = [re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}")]
