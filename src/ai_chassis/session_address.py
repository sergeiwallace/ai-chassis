"""`<session>@<host>`: the one form that names a session and its host together (ISSUE-REF).

Every surface a person reads or types uses it: the status line, notice and header lines, each
rung's `--to`, the chief-of-staff's session ids and remote launch targets. It is also the name
registered on Buzz and git-inbox. `*@<host>` is every session on a host; the machine itself is
the bare host.

The one exception is the Agent Mail board's own registered name. The pinned board
(mcp_agent_mail v0.3.4, `utils.py:166`) admits an explicit agent id only if it matches
`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`. Under the deployed default `coerce` mode, anything else is
silently swapped for a generated adjective+noun name, so `ISSUE-REF@example-project-ec2` cannot be registered.
The board keeps `<host>--<session>` and the two functions at the bottom of this module are the
only crossing between the forms. If the board ever admits `@`, `board_name` becomes `str(address)`
and `address_from_board_name` returns the name it was given.

A leaf module, importing nothing from `ai_chassis`, so every rung can share it without a cycle
(`git_inbox` -> `fleet_liveness` -> `buzz_channel`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

WILDCARD = "*"
_NAME = r"[a-z0-9][a-z0-9-]*"
NAME_RE = re.compile(rf"^{_NAME}$")
_ADDRESS_RE = re.compile(rf"^({_NAME}|\*)@({_NAME}|\*)$")

#: The Agent Mail board's separator between host and session in its registered agent name.
BOARD_SEPARATOR = "--"


class AddressError(ValueError):
    """An address outside the D-3 grammar. The message names the address."""


@dataclass(frozen=True)
class Address:
    """`slug@machine`, either side possibly `*`."""

    slug: str
    machine: str

    def __str__(self) -> str:
        return f"{self.slug}@{self.machine}"

    @property
    def is_broadcast(self) -> bool:
        return self.slug == WILDCARD


def session_address(machine: str, slug: str) -> str:
    """`<slug>@<machine>`, the display and wire form of one session."""
    return str(Address(slug=slug, machine=machine))


def _alias(text: str) -> str:
    if text in ("fleet", "fleet:*"):
        return "*@*"
    if text.startswith("machine:"):
        return f"*@{text.removeprefix('machine:')}"
    return text


def parse_address(text: str) -> Address:
    """One address in canonical form. Aliases: `machine:<m>` -> `*@<m>`, `fleet`/`fleet:*` -> `*@*`."""
    raw = text.strip()
    match = _ADDRESS_RE.match(_alias(raw))
    if match is None:
        raise AddressError(f"not a slug@machine address: {raw!r}")
    return Address(slug=match.group(1), machine=match.group(2))


def parse_addresses(text: str) -> tuple[Address, ...]:
    """A comma-separated list, de-duplicated in order. An empty list or entry is refused."""
    parts = text.split(",")
    if not text.strip():
        raise AddressError("empty address list")
    addresses: list[Address] = []
    for part in parts:
        if not part.strip():
            raise AddressError(f"empty entry in address list {text.strip()!r}")
        address = parse_address(part)
        if address not in addresses:
            addresses.append(address)
    return tuple(addresses)


def format_addresses(addresses: Iterable[Address]) -> str:
    return ", ".join(str(address) for address in addresses)


# ---------------------------------------------------------------------------------------------
# The Agent Mail board edge: the only place `<host>--<session>` is built or read.
# ---------------------------------------------------------------------------------------------


def board_name(address: Address) -> str:
    """The board agent name a concrete session address is registered under."""
    return f"{address.machine}{BOARD_SEPARATOR}{address.slug}"


def address_from_board_name(name: str) -> Address | None:
    """The session address a board agent name stands for, or None for a name with no host half.

    None covers the bare machine identity and a board-generated name (`BronzeGate`); a caller
    showing a sender renders those as they are. The halves are not re-validated: the board
    matches names case-insensitively, and a label is whatever its host registered.
    """
    machine, separator, slug = name.partition(BOARD_SEPARATOR)
    if not separator or not machine or not slug:
        return None
    return Address(slug=slug, machine=machine)


def display_name(name: str) -> str:
    """A board sender as a person should read it: `<session>@<host>`, else the name unchanged."""
    address = address_from_board_name(name)
    return name if address is None else str(address)
