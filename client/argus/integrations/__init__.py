"""Argus framework integrations (lightning, keras, hydra, ...).

Each submodule exports an ``ArgusCallback`` that wraps the Argus
:class:`~argus.Reporter` so users get auto-reporting from their existing
trainer flows without writing any boilerplate.

Imports are lazy: missing optional deps (``pytorch-lightning``, ``keras``,
``hydra-core``) do not break the rest of the SDK. Submodules are imported
on demand, e.g.::

    from argus.integrations.lightning import ArgusCallback
    from argus.integrations.keras import ArgusCallback
    from argus.integrations.hydra import ArgusCallback

Install with the matching extra::

    pip install argus-reporter[lightning]
    pip install argus-reporter[keras]
    pip install argus-reporter[hydra]
    pip install argus-reporter[all-integrations]
"""

__all__: list[str] = []
