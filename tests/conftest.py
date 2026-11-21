import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="avp-test-")
os.environ["AVP_DATA"] = _tmp + "/data"
os.environ["AVP_RUN"] = _tmp + "/run"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
