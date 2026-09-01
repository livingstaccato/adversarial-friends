import json

import pytest

from adversarial_friends.commands.resolve import _load_meta
from adversarial_friends.errors import UsageError
from adversarial_friends.jsonio import MAX_JSON_FILE_BYTES, load_json_object
from adversarial_friends.outcomes import (
    MAX_JSON_SCALAR_BYTES,
    MAX_JSON_STRING_BYTES,
    json_node_count,
)


def test_safe_loader_refuses_oversize_before_json_decode(tmp_path, monkeypatch):
    path = tmp_path / "run.json"
    path.write_bytes(b" " * (MAX_JSON_FILE_BYTES + 1))

    monkeypatch.setattr(json, "loads", lambda _value: (_ for _ in ()).throw(AssertionError()))
    with pytest.raises(UsageError, match=r"exceeds.*byte limit"):
        load_json_object(path, label="saved run metadata")


def test_safe_loader_refuses_symlink_and_invalid_utf8_stably(tmp_path):
    target = tmp_path / "target"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "run.json"
    link.symlink_to(target)
    with pytest.raises(UsageError, match="regular file"):
        load_json_object(link, label="saved run metadata")

    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"\xff")
    with pytest.raises(UsageError, match="valid UTF-8"):
        load_json_object(invalid, label="operator response")


def test_json_validation_bounds_each_string_and_all_scalar_bytes_including_keys():
    with pytest.raises(ValueError, match="string byte limit"):
        json_node_count({"value": "x" * (MAX_JSON_STRING_BYTES + 1)})

    chunk = "x" * (MAX_JSON_STRING_BYTES // 2)
    count = MAX_JSON_SCALAR_BYTES // len(chunk) + 2
    with pytest.raises(ValueError, match="aggregate scalar byte limit"):
        json_node_count({f"key-{index}": chunk for index in range(count)})


def test_resolve_metadata_uses_the_same_bounded_no_follow_loader(tmp_path):
    run_dir = tmp_path / "run-hostile"
    run_dir.mkdir()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    (run_dir / "run.json").symlink_to(target)

    with pytest.raises(UsageError, match="regular file"):
        _load_meta(run_dir)
