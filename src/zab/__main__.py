"""Support ``python -m zab`` as well as the installed console script."""

from zab.cli import main

raise SystemExit(main())
