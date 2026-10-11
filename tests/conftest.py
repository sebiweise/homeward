import pytest

from homeward.core.state import StateDB
from homeward.providers.fake import FakeProvider


@pytest.fixture
def state() -> StateDB:
    return StateDB(":memory:")


@pytest.fixture
def nas() -> FakeProvider:
    return FakeProvider("nas")


@pytest.fixture
def icloud() -> FakeProvider:
    return FakeProvider("icloud")
