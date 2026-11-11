from pathlib import Path
import pytest
from layer4 import Layer4Config

@pytest.fixture
def root(): return Path(__file__).resolve().parents[1]
@pytest.fixture
def cfg(root): return Layer4Config.from_yaml(root/'configs/configuration_boulder.yaml')
