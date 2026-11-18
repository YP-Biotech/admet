"""Every test runs against a throwaway admet home, never the user's ~/.admet."""

import atexit
import os
import shutil
import tempfile

if "ADMET_HOME" not in os.environ:
    _home = tempfile.mkdtemp(prefix="admet-test-home-")
    os.environ["ADMET_HOME"] = _home
    atexit.register(shutil.rmtree, _home, True)
