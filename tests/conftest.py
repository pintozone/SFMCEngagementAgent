import os
import sys
import tempfile
from pathlib import Path

os.environ["SFMC_KIT_DATA_DIR"] = tempfile.mkdtemp(prefix="sfmckit-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
