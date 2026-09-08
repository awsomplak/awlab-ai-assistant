"""
Build version for awlab-ai-assistant.

Single source of truth for the server version.
Bump ``__version__`` on each release.
"""

__version__ = "3.0.6"
__version_info__ = (3, 0, 6)
__build_tag__ = "build.116"  # Increment this on each build for better traceability (e.g. in logs)

VERSION_STRING = f"AWLab-AI-Assistant v{__version__}+{__build_tag__}"
