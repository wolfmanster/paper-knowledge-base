"""
MinerU-GUI version — static value kept in sync with the monorepo root ``_version.py``.

When installing from source (``pip install -e .`` or ``pip install .``), this
is read by setuptools via ``version = {attr = "_version.__version__"}`` in
``pyproject.toml`` and baked into the package METADATA.  At runtime,
``importlib.metadata.version("mineru-gui")`` is the authoritative source;
this file serves as the build-time fallback.

Usage:
    from _version import __version__
"""
__version__ = "1.0.0"
