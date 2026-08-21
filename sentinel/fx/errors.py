"""Named failures for the forex path (docs/specs/FOREX.md §12).

Every one of these degrades **forex only**. None of them is raised anywhere crypto
can reach, and each carries enough detail to name the cause in an alert without
naming a credential.
"""

from __future__ import annotations


class ForexError(Exception):
    """Base for every forex-only failure."""


class PipMismatch(ForexError):
    """The pip derived from ``Format.Decimals`` disagrees with ``TickSize x 10``.

    This is the guard against docs/specs/FOREX.md's failure mode A. The spike made
    exactly this mistake — reading v1's "5 decimals" as ``10 ** -(decimals - 1)``,
    which against the real ``Decimals=4`` yields 0.001 instead of 0.0001: every
    spread and every stop distance wrong by 10x, plausible-looking, and with no
    error anywhere to notice it by. It was caught only because the reference data
    contradicted the spec, which is precisely why the cross-check is an assertion in
    code rather than a note in a document.
    """


class InstrumentUnresolved(ForexError):
    """A configured symbol could not be resolved to a Uic, with the reason named.

    §4.2: unresolvable instruments are skipped with a named reason, never silently.
    """


class DegradedRead(ForexError):
    """The venue returned fewer candles than were requested (§4.4, D-e).

    An over-requested ``Count`` clamps to 1200 silently, and a range with fewer bars
    available returns what it has. Neither is a shorter tail we may analyse: both are
    a **degraded read**, and the symbol is skipped this cycle.
    """


class TokenPersistFailed(ForexError):
    """A refreshed token was not durably stored (§3 requirement 2).

    A 2xx from the token endpoint is not proof the token was saved. The spike hit
    exactly this: a successful exchange printed success and persisted nothing, so the
    single-use refresh token was issued and thrown away. Raised when the read-back
    does not match what was written.
    """


class ReauthenticationRequired(ForexError):
    """The refresh chain is dead; only a manual browser login can restore it (§3.1).

    The credential chain has a one-hour memory, so any outage longer than that ends
    here. The alert this produces must name re-authentication as the action and carry
    the authorize URL — a generic "forex paused" sends the owner hunting a data
    problem when the fix is a two-minute login.
    """
