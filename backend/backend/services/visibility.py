"""Visibility resolver.

Centralises the "can this user see this batch?" logic so every read
endpoint applies the same rule (requirements §7.4). The resolver
returns a SQLAlchemy :class:`Select` that callers compose into their
own query — rather than a list of ids — so JOIN + filter plans stay
efficient on large datasets.

Three share surfaces feed into visibility (migration 004):

* ``batch_share(batch_id, grantee_id)`` — single-batch grants
* ``project_share(owner_id, project, grantee_id)`` — blanket per-project
  grants that cover all of the owner's current + future batches
* ``public_share(slug, batch_id)`` — anonymous read-only URLs

The resolver folds the first two into ``scope='shared'`` / ``'all'``;
public share lookup happens in a separate endpoint (``/api/public/``)
because it short-circuits the whole auth layer.

Demo-fixture visibility (2026-04-24 flip)
-----------------------------------------

Projects flagged ``ProjectMeta.is_demo=True`` (plus the batches and
hosts attached to them) are **invisible to every authenticated
user** — admins included — by default. They surface only to anonymous
visitors through the ``/api/public/*`` routes. This is the reverse of
the earlier ``User.hide_demo`` opt-out model: the legacy flag is kept
on the user row for backwards compatibility but is no longer consulted
in any read path.

Callers that need an escape hatch (e.g. an admin auditing the demo
project directly) may pass ``include_demo=True`` to
:meth:`VisibilityResolver.visible_batches_query`; the routers do not
expose this knob today.
"""
from __future__ import annotations

import logging
from typing import Literal

from sqlalchemy import Select, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.models import (
    Batch,
    BatchShare,
    ProjectMeta,
    ProjectShare,
    PublicShare,
    User,
)

log = logging.getLogger(__name__)


Scope = Literal["mine", "shared", "all", "public"]


def _demo_project_subquery():
    """Scalar subquery yielding every project name flagged ``is_demo=True``.

    Used to filter batches out of authenticated read paths. Returning a
    subquery (rather than materialising a list) lets the outer
    ``WHERE Batch.project NOT IN (...)`` clause stay set-based so
    SQLAlchemy can plan it efficiently against the small
    ``idx_project_meta_is_demo`` index.
    """
    return select(ProjectMeta.project).where(ProjectMeta.is_demo.is_(True))


def _shared_batch_ids_subquery(user_id: int):
    """Return a scalar subquery yielding batch ids shared to ``user_id``.

    Composed from two unioned sources:

    1. ``batch_share.batch_id WHERE grantee_id = user_id``
    2. ``batch.id`` joined onto ``project_share`` where
       ``project_share.owner_id = batch.owner_id`` and
       ``project_share.project = batch.project`` and
       ``project_share.grantee_id = user_id``

    Returning the raw ids as a subquery (rather than a joined select)
    keeps the outer ``WHERE Batch.id IN (...)`` form readable and lets
    SQLAlchemy plan the inner UNION independently.
    """
    bs_part = select(BatchShare.batch_id).where(
        BatchShare.grantee_id == user_id
    )
    ps_part = (
        select(Batch.id)
        .select_from(Batch)
        .join(
            ProjectShare,
            and_(
                ProjectShare.owner_id == Batch.owner_id,
                ProjectShare.project == Batch.project,
            ),
        )
        .where(ProjectShare.grantee_id == user_id)
    )
    return bs_part.union(ps_part)


class VisibilityResolver:
    """Compute the subset of batches a user is allowed to see."""

    async def visible_batches_query(
        self,
        user: User,
        scope: Scope = "all",
        db: AsyncSession | None = None,  # reserved for future joins
        include_demo: bool = False,
    ) -> Select:
        """Return a ``SELECT Batch`` statement filtered by visibility.

        Parameters
        ----------
        user:
            The authenticated user whose view we're computing.
        scope:
            Viewport selector — ``'mine'`` | ``'shared'`` | ``'all'`` |
            ``'public'``.
        include_demo:
            When ``False`` (default) every batch whose ``project`` is
            tagged ``ProjectMeta.is_demo=True`` is excluded, regardless
            of scope or admin status. When ``True`` the demo filter is
            skipped — used as an admin escape hatch (``?include_demo=
            true`` on the few routers that opt in).

        Semantics
        ---------
        * ``mine`` — ``owner_id == user.id``
        * ``shared`` — batches shared to the user via ``batch_share``
          or ``project_share``. Admins also go through this path so
          they see the same rows a non-admin grantee would — ``all``
          is the right scope for "everything".
        * ``all`` — ``mine ∪ shared``. For admin users this degrades
          into "no filter" so they can triage across the whole system.
        * ``public`` — batches with a non-expired ``public_share`` row.

        Soft-deleted batches (``is_deleted=True``) are excluded from
        every scope except when the caller explicitly overrides via
        a post-hoc ``.where`` clause.
        """
        stmt = select(Batch).where(Batch.is_deleted.is_(False))

        # Demo fixture filter (2026-04-24 flip): logged-in users never
        # see demo projects by default. Apply the filter before the
        # scope-specific predicates so every branch inherits it — the
        # demo project is invisible whether the user is on
        # ``scope='mine'`` (a non-demo user won't own demo batches
        # anyway), ``scope='shared'`` (no-one ever grants access to
        # the demo), or ``scope='all'``.
        if not include_demo:
            stmt = stmt.where(Batch.project.notin_(_demo_project_subquery()))

        if scope == "mine":
            # Explicit "mine" means what it says, even for admins.
            return stmt.where(Batch.owner_id == user.id)

        if scope == "shared":
            shared_ids = _shared_batch_ids_subquery(user.id)
            return stmt.where(Batch.id.in_(shared_ids))

        if scope == "public":
            public_ids = select(PublicShare.batch_id)
            return stmt.where(Batch.id.in_(public_ids))

        # scope == "all"
        if user.is_admin:
            # Admins sweep the whole surface for triage — with demo
            # excluded unless they explicitly opt in via include_demo.
            return stmt
        shared_ids = _shared_batch_ids_subquery(user.id)
        return stmt.where(
            or_(Batch.owner_id == user.id, Batch.id.in_(shared_ids))
        )

    async def can_view_batch(
        self, user: User, batch_id: str, db: AsyncSession
    ) -> bool:
        """True iff the user is allowed to read ``batch_id``.

        Separate from :meth:`visible_batches_query` because detail
        endpoints need a boolean, not a SELECT.

        Demo-fixture batches (whose project is flagged ``is_demo=True``
        on :class:`ProjectMeta`) are invisible to every authenticated
        caller, admins included. Anonymous visitors reach the demo via
        the separate ``/api/public/*`` surface, which does not call this
        method.
        """
        row = await db.get(Batch, batch_id)
        if row is None or row.is_deleted:
            return False
        # Demo short-circuit — applies before admin / owner / share
        # checks so even the seeded ``user='demo'`` owner (if a real
        # account ever shared that username) can't re-enter via direct
        # navigation. The demo lookup is a cheap single-row get; no
        # extra join on the hot path because we only hit it for the
        # one seeded project.
        if row.project is not None:
            meta = await db.get(ProjectMeta, row.project)
            if meta is not None and meta.is_demo:
                return False
        if user.is_admin:
            return True
        if row.owner_id == user.id:
            return True
        # batch-level share
        bs = await db.get(BatchShare, (batch_id, user.id))
        if bs is not None:
            return True
        # project-level share (owner × project × grantee)
        if row.owner_id is not None:
            ps = await db.get(
                ProjectShare, (row.owner_id, row.project, user.id)
            )
            if ps is not None:
                return True
        return False

    async def can_edit_batch(
        self, user: User, batch_id: str, db: AsyncSession
    ) -> bool:
        """True iff the user is allowed to mutate ``batch_id``.

        Owners + admins always edit. Shared users edit only when their
        share row has ``permission='editor'``. Demo batches are
        read-only for everyone — they always return False so no
        authenticated action (rerun, stop, delete, share grant) can
        mutate the public fixture.
        """
        row = await db.get(Batch, batch_id)
        if row is None or row.is_deleted:
            return False
        if row.project is not None:
            meta = await db.get(ProjectMeta, row.project)
            if meta is not None and meta.is_demo:
                return False
        if user.is_admin:
            return True
        if row.owner_id == user.id:
            return True
        bs = await db.get(BatchShare, (batch_id, user.id))
        if bs is not None and bs.permission == "editor":
            return True
        if row.owner_id is not None:
            ps = await db.get(
                ProjectShare, (row.owner_id, row.project, user.id)
            )
            if ps is not None and ps.permission == "editor":
                return True
        return False
