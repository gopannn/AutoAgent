"""Compatibility facade for ctd.kernel.query."""
from ctd.kernel import query as _impl
globals().update({k: v for k, v in vars(_impl).items() if k not in {"__name__", "__package__", "__loader__", "__spec__"}})
