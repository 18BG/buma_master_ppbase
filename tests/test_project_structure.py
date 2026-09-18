from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_required_foundation_paths_exist() -> None:
    required = [
        "README.md",
        "pyproject.toml",
        ".env.example",
        "docker-compose.yml",
        "Makefile",
        "app/hooks/register.py",
        "app/hooks/health.py",
        "pb_migrations",
        "docs/implementation-plan.md",
        "docs/migration-ppbase-tasklist.md",
        "docs/source-manifest.json",
    ]
    missing = [path for path in required if not (ROOT / path).exists()]
    assert missing == []
