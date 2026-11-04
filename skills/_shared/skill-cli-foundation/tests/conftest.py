# `pytest_plugins` moved to the repo-root conftest.py — pytest 9 no longer allows a
# non-top-level conftest.py to declare `pytest_plugins` (it would silently affect the
# entire suite, not just this subtree, which is exactly what this declaration needs
# since `tests/test_usage_status.py` and others outside this package also rely on the
# `cli_runner` fixture from `skill_cli_foundation.testing`).
