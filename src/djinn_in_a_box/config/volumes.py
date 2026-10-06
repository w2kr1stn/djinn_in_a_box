"""Internal identities are never selectable backup or ordinary-clean resources."""

HOSTCTL_STATE_VOLUME = "djinn-hostctl-state"
PROTECTED_INTERNAL_VOLUMES = frozenset({HOSTCTL_STATE_VOLUME})
