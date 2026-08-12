"""PyInstaller entry point.

Deliberately kept OUTSIDE the kitsu_standalone_app package and as thin as
possible: PyInstaller's Analysis() executes this file as a top-level script
(__name__ == "__main__", no parent package), so it must import the app as a
package (absolute import) rather than live inside it — kitsu_standalone_app/
main.py itself uses relative imports (`from .login_window import ...`),
which only resolve when it's imported as part of the package (as this does,
and as `python -m kitsu_standalone_app.main` does), not when run directly.
"""

from kitsu_standalone_app.main import main

if __name__ == "__main__":
    main()
