import pytest

@pytest.fixture
def broken_setup():
    raise RuntimeError("intentional setup probe")

@pytest.fixture
def skipped_setup():
    pytest.skip("intentional setup skip")

@pytest.fixture
def broken_teardown():
    yield
    raise RuntimeError("intentional teardown probe")

def test_pass():
    assert 2 + 2 == 4

def test_call_failure():
    assert False, "intentional call failure"

def test_call_skip():
    pytest.skip("intentional call skip")

def test_setup_failure(broken_setup):
    pass

def test_setup_skip(skipped_setup):
    pass

def test_teardown_failure(broken_teardown):
    assert True

@pytest.mark.xfail(reason="intentional unexpected-pass probe", strict=False)
def test_unexpected_pass():
    assert True
