"""Resolve this machine's AWS SSO profile name from ``~/.aws/config`` at CALL TIME.

WHY THIS EXISTS (ISSUE-REF)
---------------------------
Three hardcoded profile defaults have gone stale in turn on this fleet, and each time the
caller logged a tidy skip and did nothing: an account-id-first name at the ISSUE-REF rename,
then `example-client-host-semantic-kg-dev`/`example-permission-set` on example-client-host-windows, then both of those at the
ISSUE-REF rename to `{suffix}-{permset}-{id}`. The maintainer's ruling on this class of defect is that
it gets fixed systematically rather than documented: *"you shouldn't have to remember to ever
do this."*

The names above are deliberately given as SHAPES rather than spelled out: this module is the
one place that must carry no profile literal, and
`tests/test_no_hardcoded_aws_profile_names.py` enforces that on this file too.

So no harness caller names a profile. Every caller asks this module, which reads the live
`~/.aws/config` and returns whatever is configured there. A rename of the live profile is
then a no-op for every caller, which is the property that makes the rename itself safe to
run while nothing is holding a credential.

WHAT IS A SECRET HERE AND WHAT IS NOT
-------------------------------------
A profile NAME is not a secret, and this module returns one. An AWS account id is carried
INSIDE the name, which is why a resolved name belongs in a gitignored overlay or an
environment, never in a tracked file -- and why this module reads the id rather than letting
any caller hold it as a literal.

THE CONVENTION, AND WHY CONFORMANCE IS DERIVABLE WITHOUT A NETWORK CALL
-----------------------------------------------------------------------
`docs/procedures/aws-sso-profile-naming.md` (status: active) specifies

    {account_name_suffix}-{permset_alias}-{account_id}

where `permset_alias` strips `SSO-MV-` and `-PermissionSet` from the IAM Identity Center
permission set name and lowercases the remainder (`SSO-MV-FullAccess` -> `fullaccess`).

The `account_name_suffix` comes from the AWS account DISPLAY name, which is not in
`~/.aws/config` -- recovering it needs `aws sso list-accounts`, a network call with a live
token. But the other two fields are both in the profile's own section (`sso_account_id` and
`sso_role_name`), so the convention's TAIL is derivable offline, and that is enough to decide
whether a configured name conforms and what it should be renamed to. Nothing here calls
`aws`, mints a credential, or reaches the network.
"""

from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path

#: Stripped from the permission set name before lowercasing, per the convention's field table.
PERMSET_STRIP_PREFIX = "SSO-MV-"
PERMSET_STRIP_SUFFIX = "-PermissionSet"

#: Characters the convention collapses away inside a permission set alias, so a configured
#: `full-access` is recognisable as the same alias as the conventional `fullaccess`. This is
#: what lets `conventional_name` propose a rename instead of merely reporting a mismatch.
_ALIAS_SEPARATORS = "-_ "


class ProfileResolutionError(Exception):
    """The AWS profile name could not be resolved from the local configuration.

    Raised rather than returning a placeholder: a caller that writes a guessed profile name
    into a config file produces a credential lookup that fails later, somewhere else, with no
    trace back to the guess. Every caller here would rather refuse and name the reason.
    """


@dataclass(frozen=True)
class SsoProfile:
    """One SSO-backed profile section of ``~/.aws/config``."""

    name: str
    account_id: str
    role_name: str
    sso_session: str

    @property
    def permset_alias(self) -> str:
        """The convention's `permset_alias` for this profile's permission set."""
        return permset_alias(self.role_name)

    @property
    def conventional_tail(self) -> str:
        """The `-{permset_alias}-{account_id}` every conforming name ends with."""
        return f"-{self.permset_alias}-{self.account_id}"

    @property
    def conforms(self) -> bool:
        """Whether the configured name matches the documented convention."""
        return self.name.endswith(self.conventional_tail)

    @property
    def conventional_name(self) -> str:
        """What this profile would be called under the convention.

        Equal to ``name`` when the name already conforms. Otherwise the configured alias
        segment is replaced by the conventional one -- the only edit the convention asks for
        on a name that is already suffix-first and id-last.

        Raises:
            ProfileResolutionError: the configured name is not shaped
                ``<suffix>-<alias>-<account_id>`` at all, so no single-segment rewrite would
                make it conform and proposing one would be a guess.
        """
        if self.conforms:
            return self.name
        id_tail = f"-{self.account_id}"
        if not self.name.endswith(id_tail):
            raise ProfileResolutionError(
                f"profile {self.name!r} does not end in its own account id, so it predates "
                f"the {'{suffix}-{permset_alias}-{account_id}'} convention entirely; rename "
                "it by hand per docs/procedures/aws-sso-profile-naming.md"
            )
        prefix = self.name[: -len(id_tail)]
        alias = self.permset_alias
        suffix = _strip_alias_segment(prefix, alias)
        if suffix is None:
            raise ProfileResolutionError(
                f"profile {self.name!r} carries no segment matching permission set "
                f"{self.role_name!r} (alias {alias!r}), so the conventional name cannot be "
                "derived from it; rename it by hand per "
                "docs/procedures/aws-sso-profile-naming.md"
            )
        return f"{suffix}-{alias}-{self.account_id}"


def permset_alias(role_name: str) -> str:
    """The convention's lowercase alias for an IAM Identity Center permission set name."""
    alias = role_name.strip()
    if alias.startswith(PERMSET_STRIP_PREFIX):
        alias = alias[len(PERMSET_STRIP_PREFIX) :]
    if alias.endswith(PERMSET_STRIP_SUFFIX):
        alias = alias[: -len(PERMSET_STRIP_SUFFIX)]
    return alias.lower()


def _normalise_alias(text: str) -> str:
    """``text`` with the convention's separators collapsed away, lowercased."""
    for separator in _ALIAS_SEPARATORS:
        text = text.replace(separator, "")
    return text.lower()


def _strip_alias_segment(prefix: str, alias: str) -> str | None:
    """``prefix`` without its trailing alias segment, or None when it carries none.

    The configured spelling of the alias may differ from the conventional one only by the
    separators the convention strips (`full-access` against `fullaccess`), so the comparison
    is made on the normalised forms. The SHORTEST matching tail wins: a longer one would eat
    part of the account name suffix.
    """
    target = _normalise_alias(alias)
    for cut in range(len(prefix) - 1, 0, -1):
        if prefix[cut] != "-":
            continue
        if _normalise_alias(prefix[cut + 1 :]) == target:
            return prefix[:cut]
    return None


def config_path(env: dict[str, str] | None = None) -> Path:
    """Where the AWS CLI reads profile configuration from, honouring its own override."""
    environ = os.environ if env is None else env
    configured = environ.get("AWS_CONFIG_FILE")
    return Path(configured) if configured else Path.home() / ".aws" / "config"


def load_sso_profiles(path: Path | None = None) -> list[SsoProfile]:
    """Every SSO-backed profile in the AWS config, in file order.

    A profile is SSO-backed when it carries an `sso_session`; those are the only ones a
    profile name can be resolved for here, and the only ones whose account and permission set
    are readable without a credential call. Returns empty for an absent or unparseable file
    rather than raising -- the caller distinguishes "no profiles" from "no config" by its own
    error text, and a parse error in someone's hand-edited config should not crash a hook.
    """
    target = config_path() if path is None else path
    parser = configparser.RawConfigParser()
    try:
        parser.read(target, encoding="utf-8")
    except (OSError, configparser.Error):
        return []
    profiles: list[SsoProfile] = []
    for section in parser.sections():
        if not section.startswith("profile "):
            continue
        if not parser.has_option(section, "sso_session"):
            continue
        account_id = parser.get(section, "sso_account_id", fallback="").strip()
        role_name = parser.get(section, "sso_role_name", fallback="").strip()
        if not account_id or not role_name:
            continue
        profiles.append(
            SsoProfile(
                name=section[len("profile ") :].strip(),
                account_id=account_id,
                role_name=role_name,
                sso_session=parser.get(section, "sso_session", fallback="").strip(),
            )
        )
    return profiles


def resolve(
    path: Path | None = None,
    *,
    env: dict[str, str] | None = None,
    account_suffix: str | None = None,
) -> SsoProfile:
    """This machine's SSO profile, read from the live AWS configuration.

    Selection, in order:

    1. ``account_suffix``, when given: the profile whose name starts with it. A caller that
       needs a specific account names the account's own display suffix, which carries no
       account id and so may live in a tracked file.
    2. ``AWS_PROFILE`` from ``env``, when it names a profile that actually exists. This is the
       profile the calling session already authenticates with, so a caller inherits the
       operator's choice rather than re-deciding it.
    3. The single SSO profile in the file, when there is exactly one.

    Raises:
        ProfileResolutionError: nothing matched, or the selection was ambiguous. Ambiguity
            FAILS rather than picking the first: on a machine with several accounts
            configured, picking one silently is how a tunnel opens against the wrong account.
    """
    profiles = load_sso_profiles(path)
    where = config_path(env) if path is None else path
    if not profiles:
        raise ProfileResolutionError(
            f"{where} declares no SSO-backed profile (a profile with an sso_session, an "
            "sso_account_id and an sso_role_name), so this machine's AWS profile name cannot "
            "be derived; run `aws configure sso` per "
            "docs/procedures/aws-sso-profile-naming.md"
        )
    if account_suffix:
        matches = [p for p in profiles if p.name.startswith(f"{account_suffix}-")]
        return _exactly_one(matches, where, f"account suffix {account_suffix!r}")
    environ = os.environ if env is None else env
    named = (environ.get("AWS_PROFILE") or "").strip()
    if named:
        matches = [p for p in profiles if p.name == named]
        if matches:
            return matches[0]
    return _exactly_one(profiles, where, "no selector")


def _exactly_one(matches: list[SsoProfile], where: Path, selector: str) -> SsoProfile:
    """The one match, or a refusal naming what was found."""
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ProfileResolutionError(
            f"no SSO profile in {where} matches {selector}, so the AWS profile name cannot "
            "be derived"
        )
    raise ProfileResolutionError(
        f"{selector} matches {len(matches)} SSO profiles in {where} "
        f"({', '.join(sorted(p.name for p in matches))}); pass an account suffix or set "
        "AWS_PROFILE so the choice is the operator's rather than this module's"
    )


def resolve_name(
    path: Path | None = None,
    *,
    env: dict[str, str] | None = None,
    account_suffix: str | None = None,
) -> str:
    """The configured profile NAME, for a caller that needs nothing else."""
    return resolve(path, env=env, account_suffix=account_suffix).name
