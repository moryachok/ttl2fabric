import shutil
from pathlib import Path

import pytest

from ttl2fabric.catalog import FileCatalog, NullCatalog
from ttl2fabric.model import LakehouseRef
from ttl2fabric.parser import parse_ttl
from ttl2fabric.resolver import Options, resolve

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def source():
    return parse_ttl(str(FIXTURES / "mini.ttl"))


@pytest.fixture
def catalog():
    return FileCatalog(str(FIXTURES / "catalog.json"))


@pytest.fixture
def lakehouse():
    return LakehouseRef(
        workspace_id="11111111-1111-1111-1111-111111111111",
        workspace_name="test-ws",
        lakehouse_id="22222222-2222-2222-2222-222222222222",
        lakehouse_name="test_lakehouse",
    )


@pytest.fixture
def null_catalog(lakehouse):
    return NullCatalog(lakehouse)


@pytest.fixture
def run(source, catalog):
    def _run(**kwargs):
        cat = kwargs.pop("catalog", catalog)
        kwargs.setdefault("schemas", ["bronze"])
        return resolve(source, cat, Options(**kwargs), "TestOnto")

    return _run


@pytest.fixture
def fixtures_dir(tmp_path):
    target = tmp_path / "fixtures"
    shutil.copytree(FIXTURES, target)
    return target
