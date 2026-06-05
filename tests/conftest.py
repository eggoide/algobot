import os
import sys

# Make `app/` importable without installing as a package.
_HERE = os.path.dirname(os.path.abspath(__file__))
_APP = os.path.normpath(os.path.join(_HERE, os.pardir, "app"))
if _APP not in sys.path:
    sys.path.insert(0, _APP)
