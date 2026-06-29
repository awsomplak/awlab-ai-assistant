"""
Framework detection, project scanning, and fingerprinting.

Implements the 06-project-scanner fingerprint protocol in Python.
"""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ...config import settings
from ...helpers import read_utf8
from ._cache import load_cache, save_cache

# ── Cache file path ─────────────────────────────────────────────────────────

_SCAN_CACHE_FILE = "scan_cache.json"

# ── Framework Detection Tables (06-project-scanner protocol) ─────────────────

_FRAMEWORK_DETECTORS: list[tuple[str, list[str], str]] = [
    # (category, detection_files, result)
    ("monorepo", ["pnpm-workspace.yaml"], "pnpm Workspaces"),
    ("monorepo", ["lerna.json"], "Lerna"),
    ("monorepo", ["nx.json"], "Nx"),
    ("monorepo", ["turbo.json"], "Turborepo"),
    ("framework", ["next.config.*"], "Next.js"),
    ("framework", ["nuxt.config.*"], "Nuxt"),
    ("framework", ["angular.json"], "Angular"),
    ("framework", ["vite.config.*"], "Vite"),
    ("framework", ["artisan"], "Laravel"),
    ("framework", ["manage.py"], "Django"),
    ("framework", ["astro.config.*"], "Astro"),
    ("mobile", ["pubspec.yaml"], "Flutter (pubspec)"),
    ("mobile", ["react-native.config.js", "app.json"], "React Native"),
    ("mobile", ["capacitor.config.ts"], "Capacitor"),
    ("language", ["package.json"], "Node.js/JavaScript/TypeScript"),
    ("language", ["composer.json"], "PHP"),
    ("language", ["pubspec.yaml"], "Dart"),
    ("language", ["Cargo.toml"], "Rust"),
    ("language", ["go.mod"], "Go"),
    ("language", ["requirements.txt", "pyproject.toml", "setup.py"], "Python"),
    ("language", ["Gemfile"], "Ruby"),
    ("language", ["pom.xml", "build.gradle"], "Java/Kotlin"),
    ("language", ["*.csproj", "*.sln"], "C#/.NET"),
    ("test", ["jest.config.*"], "Jest"),
    ("test", ["vitest.config.*"], "Vitest"),
    ("test", ["phpunit.xml"], "PHPUnit"),
    ("test", ["pytest.ini", "conftest.py"], "Pytest"),
    ("test", ["*.test.dart", "test/"], "Flutter Test"),
    ("cicd", [".github/workflows/"], "GitHub Actions"),
    ("cicd", [".gitlab-ci.yml"], "GitLab CI"),
    ("cicd", ["Jenkinsfile"], "Jenkins"),
    ("cicd", [".circleci/"], "CircleCI"),
]

_SCAN_TARGETS: dict[str, list[str]] = {
    "Laravel": ["app/Models/", "app/Http/Controllers/", "routes/", "database/migrations/", "config/"],
    "Django": ["*/models.py", "*/views.py", "*/urls.py", "*/serializers.py"],
    "Next.js": ["app/", "pages/", "components/", "lib/", "api/"],
    "Nuxt": ["pages/", "components/", "composables/", "server/"],
    "Angular": ["src/app/", "src/environments/"],
    "Vite": ["src/", "components/", "hooks/", "lib/"],
    "React Native": ["src/", "screens/", "components/", "navigation/"],
    "Flutter (pubspec)": ["lib/models/", "lib/screens/", "lib/pages/", "lib/providers/", "lib/bloc/", "lib/services/"],
    "Node.js/JavaScript/TypeScript": ["src/", "lib/", "config/", "test/"],
    "Python": ["src/", "lib/", "config/", "test/"],
}


# ── Framework Detection ─────────────────────────────────────────────────────


def detect_framework(workspace_path: str) -> dict[str, Any]:
    """Detect project framework using the 06-project-scanner fingerprint protocol."""
    root = Path(workspace_path)
    detected: dict[str, list[str]] = {}

    for category, patterns, result_name in _FRAMEWORK_DETECTORS:
        for pattern in patterns:
            # Handle glob patterns
            if "*" in pattern or "?" in pattern:
                matches = list(root.glob(pattern))
                if matches:
                    detected.setdefault(category, []).append(result_name)
                    break
            # Handle directory existence
            elif pattern.endswith("/"):
                if (root / pattern).is_dir():
                    detected.setdefault(category, []).append(result_name)
                    break
            # Handle file existence
            else:
                if (root / pattern).exists():
                    detected.setdefault(category, []).append(result_name)
                    break

    # Determine primary framework (first framework entry wins)
    frameworks = detected.get("framework", [])
    primary_framework = frameworks[0] if frameworks else "Unknown"

    # Determine scan targets
    targets = _SCAN_TARGETS.get(primary_framework, ["src/", "lib/", "config/", "test/"])

    # Check which scan targets actually exist
    existing_targets = []
    for target in targets:
        target_path = root / target
        if target_path.exists():
            existing_targets.append(target)

    return {
        "framework": primary_framework,
        "all_detected": {
            "languages": detected.get("language", []),
            "test_frameworks": detected.get("test", []),
            "frameworks": detected.get("framework", []),
            "monorepo": detected.get("monorepo", []),
            "mobile": detected.get("mobile", []),
            "cicd": detected.get("cicd", []),
        },
        "scan_targets": existing_targets,
        "languages": detected.get("language", []),
        "test_frameworks": detected.get("test", []),
        "cicd": detected.get("cicd", []),
        "monorepo": detected.get("monorepo", []),
        "mobile": detected.get("mobile", []),
    }


# ── Scan Caching ────────────────────────────────────────────────────────────


def _is_cache_valid(cached_data: dict[str, Any]) -> bool:
    """Check if cached scan data is less than 1 hour old."""
    scan_time = cached_data.get("scan_time", "")
    if not scan_time:
        return False
    try:
        scan_dt = datetime.fromisoformat(scan_time.replace("Z", "+00:00"))
        age = datetime.now(timezone.utc) - scan_dt
        return age < timedelta(hours=1)
    except (ValueError, TypeError):
        return False


# ── Tool Implementations ────────────────────────────────────────────────────


async def scan_project(
    workspace_path: str | Path = "",
    force_refresh: bool = False,
) -> dict[str, Any]:
    """
    Scan a project to detect framework, entry points, and relationships.

    Args:
        workspace_path: Project root path. If empty, falls back to CWD.
        force_refresh: If True, bypasses cache and forces a fresh scan.

    Returns:
        { success, framework, targets, relationships, entry_points, cached }
    """
    # Check cache first (< 1 hour old) — unless force_refresh is set
    cache_file_path = get_cache_path(workspace_path=workspace_path)
    cache = load_cache(workspace_path=workspace_path, cache_path=cache_file_path)
    cache_key = "last_scan"
    if not force_refresh and cache is not None:
        if cache_key in cache:
            cached_data = cache[cache_key]
            if _is_cache_valid(cached_data):
                return {**cached_data, "cached": True}

    project_root = str(workspace_path) if workspace_path else str(Path.cwd())

    # Step 1: Detect project type
    framework_info = detect_framework(project_root)

    # Step 2: Determine scan targets and read entry points
    entry_points: dict[str, list[str]] = {}
    relationships: list[dict[str, Any]] = []

    root = Path(project_root)
    for target in framework_info.get("scan_targets", []):
        target_path = root / target
        if target_path.is_dir():
            try:
                files = [str(f.relative_to(root)) for f in target_path.iterdir() if f.is_file()]
                entry_points[target] = files[:10]  # Limit to 10 files per target

                # Read up to 3 entry points (top 20 lines for import detection)
                for f in files[:3]:
                    full_path = root / f
                    if full_path.exists() and full_path.suffix in {".py", ".js", ".ts", ".jsx", ".tsx", ".php", ".rb", ".go", ".rs", ".java"}:
                        content = read_utf8(str(full_path))
                        if content:
                            lines = content.splitlines()[:20]
                            imports = [
                                l.strip() for l in lines
                                if l.strip().startswith(("import ", "from ", "use ", "require(", "#include", "use ", "package "))
                            ]
                            if imports:
                                relationships.append({
                                    "file": f,
                                    "imports": imports[:10],
                                })
            except (PermissionError, OSError):
                pass

    result = {
        "success": True,
        "framework": framework_info["framework"],
        "all_detected": framework_info.get("all_detected", {}),
        "targets": framework_info.get("scan_targets", []),
        "relationships": relationships,
        "entry_points": entry_points,
        "cached": False,
        "scan_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "project_root": project_root,
    }

    # Cache the result
    save_cache(workspace_path=workspace_path, cache_path=cache_file_path, data={cache_key: result})

    return result


# ── Helper ─────────────────────────────────────────────────────────────────


def get_cache_path(workspace_path: str | Path, cache_file: str | None = None) -> str:
    """Return the relative cache path for scan results."""
    ai_path = settings.get_ai_dir(workspace_path=workspace_path)
    memory_bank_path = settings.get_memory_bank_dir(workspace_path=workspace_path)
    file = cache_file if isinstance(cache_file, str) else _SCAN_CACHE_FILE
    cache_resolved_path = memory_bank_path / "memory" / file
    cache_path = cache_resolved_path.relative_to(ai_path)
    return str(cache_path)