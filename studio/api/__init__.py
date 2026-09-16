"""The Nimbus HTTP API.

Requires the optional extra:  pip install "ahoos-model-studio[api]"
"""

from .app import create_app

__all__ = ["create_app"]
