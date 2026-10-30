"""NemApi – passerelle locale compatible OpenAI (proxy FastAPI)."""

# Version of the application (the one displayed in the UI and the manifest).
__version__ = "5.4.2"
# Version of the OpenAI-compatible API surface. It changes only when the
# contract exposed to the clients changes, not on every release: /health
# returns both ("version" and "app_version") so they are never confused.
API_VERSION = "4.1"
