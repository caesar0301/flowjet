"""Allow ``python -m flowjet`` (defaults to the CLI)."""

from __future__ import annotations

from flowjet.cli.cli import main

raise SystemExit(main())
