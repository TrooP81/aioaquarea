from pathlib import Path

import aioaquarea


def test_py_typed_marker_exists_for_package_typing() -> None:
    package_root = Path(aioaquarea.__file__).resolve().parent

    assert (package_root / "py.typed").is_file()


def test_public_exports_use_typed_tuple() -> None:
    assert isinstance(aioaquarea.__all__, tuple)
    assert "Client" in aioaquarea.__all__
    assert "SpecialStatus" in aioaquarea.__all__
