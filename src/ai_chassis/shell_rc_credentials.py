"""Shell rc files that export a credential-named variable (ISSUE-REF).

On Fedora, bash sources ``~/.bashrc`` for a non-interactive ``bash -c`` whose stdin is a socket,
which is how Codex starts every tool command. A credential exported there therefore reaches a
delegated leaf's shell even when ``cx`` hands Codex ``shell_environment_policy.inherit="none"``.
The fix is to keep credentials in the consuming tool's own file storage and out of the rc files,
and this check keeps it that way. It reports variable NAMES only, never a value, and never the
line that holds one.
"""

from __future__ import annotations

import re
from pathlib import Path

#: Files a shell reads at startup. A missing one is simply skipped.
RC_FILES = (
    ".bashrc",
    ".bash_profile",
    ".bash_login",
    ".profile",
    ".zshenv",
    ".zprofile",
    ".zshrc",
)

#: Names that hold a secret. Deliberately narrower than a substring match on AUTH or SESSION, so
#: SSH_AUTH_SOCK (a socket path) is not reported.
_CREDENTIAL_NAME = re.compile(
    r"TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|CREDENTIAL|DSN|COOKIE|DATABASE_URL"
    r"|(^|_)API_?KEY($|_)|(^|_)PRIVATE_?KEY($|_)|(^|_)KEY$|(^|_)PAT($|_)",
    re.IGNORECASE,
)
_EXPORT_LINE = re.compile(
    r"^\s*(?:export|declare\s+-[a-zA-Z]*x[a-zA-Z]*|typeset\s+-[a-zA-Z]*x[a-zA-Z]*)\s+(.*)$"
)
_ASSIGNED_NAME = re.compile(r"(?:^|\s)([A-Za-z_][A-Za-z0-9_]*)=")

REMEDIATION = (
    "keep the credential in the consuming tool's file storage "
    "(for gh: `gh auth login --with-token`) and delete the export"
)


def credential_exports(home: Path) -> list[tuple[str, str]]:
    """``(rc file name, variable name)`` for every credential-named export, names only."""
    found: list[tuple[str, str]] = []
    for name in RC_FILES:
        path = home / name
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if line.lstrip().startswith("#"):
                continue
            export = _EXPORT_LINE.match(line)
            if export is None:
                continue
            for variable in _ASSIGNED_NAME.findall(export.group(1)):
                if _CREDENTIAL_NAME.search(variable) and (name, variable) not in found:
                    found.append((name, variable))
    return found


def findings(home: Path) -> list[str]:
    """One doctor line per credential-named export; empty when the rc files are clean."""
    return [
        f"shell rc credential: ~/{rc_file} exports {variable}; {REMEDIATION}"
        for rc_file, variable in credential_exports(home)
    ]
