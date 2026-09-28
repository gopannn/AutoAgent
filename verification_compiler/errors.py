class InfrastructureError(RuntimeError):
    """The compiler's own environment is broken (Docker, images, resolver, tooling).

    Terminal and never charged to the builder's repair budget: no code change can fix it.
    """
