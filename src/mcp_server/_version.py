"""
Build version for awlab-mcp-server.

Single source of truth for the server version.
Bump ``__version__`` on each release.
"""

__version__ = "3.0.1"
__version_info__ = (3, 0, 1)
__build_tag__ = "build.093"  # Increment this on each build for better traceability (e.g. in logs)

VERSION_STRING = f"awlab-mcp-server v{__version__}+{__build_tag__}"
