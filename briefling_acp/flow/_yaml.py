"""The one YAML entry point of the package: Hermes's YAML 1.1 loader, else PyYAML (also YAML 1.1).

YAML 1.1 matters: a bare `on:` key of a flow parses as the boolean True (see config.load_flow).
"""
try:
    from hermes_yaml import YAMLError, safe_load
except ImportError:                                   # outside Hermes
    from yaml import YAMLError, safe_load             # type: ignore[no-redef]

__all__ = ["YAMLError", "safe_load"]
