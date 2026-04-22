"""
Unit tests for pure helpers in sync/sync.py.

These tests exercise logic with no git I/O and no network — they run in
milliseconds and are the first line of defence against regressions in
parsing, hashing, and state-key formatting.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ---------- prefix_subject ----------


def test_prefix_subject_adds_tag_to_subject_only(sync_mod):
    msg = "fix: foo\n\nlong body here\n"
    out = sync_mod.prefix_subject(msg, "src-a")
    assert out.splitlines()[0] == "[src-a] fix: foo"
    # Body is untouched.
    assert "\n\nlong body here\n" in out


def test_prefix_subject_is_idempotent(sync_mod):
    """Re-prefixing an already-prefixed message is a no-op — this is what
    keeps re-runs from stacking `[x] [x] [x] subject`."""
    msg = "[src-a] fix: foo\n\nbody\n"
    assert sync_mod.prefix_subject(msg, "src-a") == msg


def test_prefix_subject_different_source_does_stack(sync_mod):
    """Idempotence is scoped to THIS source name. A different source
    legitimately layers its own prefix (we don't normally call this in
    practice, but the guarantee should be explicit)."""
    msg = "[src-a] fix: foo\n"
    assert sync_mod.prefix_subject(msg, "src-b").startswith("[src-b] [src-a] fix:")


def test_prefix_subject_empty_message_returned_asis(sync_mod):
    assert sync_mod.prefix_subject("", "src") == ""


def test_prefix_subject_single_line_no_body(sync_mod):
    out = sync_mod.prefix_subject("single line", "x")
    assert out == "[x] single line"


# ---------- Source model ----------


def test_source_from_dict_strips_slashes(sync_mod):
    src = sync_mod.Source.from_dict(
        {
            "name": "n",
            "url": "https://example.invalid/a.git",
            "src_path": "/a/b/",
            "dest_path": "/x/y/",
        }
    )
    assert src.src_path == "a/b"
    assert src.dest_path == "x/y"
    assert src.branch == "main"


def test_source_from_dict_missing_fields(sync_mod):
    with pytest.raises(ValueError, match="missing fields"):
        sync_mod.Source.from_dict({"name": "n"})


def test_source_state_key_stable(sync_mod):
    src = sync_mod.Source.from_dict(
        {
            "name": "n",
            "url": "u",
            "src_path": "a",
            "dest_path": "b",
        }
    )
    assert src.state_key == "n::a->b"


# ---------- load_sources_from_dir ----------


def test_load_sources_single_doc_per_file(tmp_path, sync_mod):
    d = tmp_path / "sources"
    d.mkdir()
    (d / "a.yaml").write_text(
        "name: a\nurl: https://e.invalid/a\nsrc_path: s\ndest_path: d\n"
    )
    (d / "b.yaml").write_text(
        "name: b\nurl: https://e.invalid/b\nsrc_path: s2\ndest_path: d2\n"
    )
    srcs = sync_mod.load_sources_from_dir(d)
    assert [s.name for s in srcs] == ["a", "b"]


def test_load_sources_list_style(tmp_path, sync_mod):
    d = tmp_path / "sources"
    d.mkdir()
    (d / "multi.yaml").write_text(
        "sources:\n"
        "  - name: one\n"
        "    url: u1\n"
        "    src_path: s\n"
        "    dest_path: d\n"
        "  - name: two\n"
        "    url: u2\n"
        "    src_path: s\n"
        "    dest_path: d2\n"
    )
    srcs = sync_mod.load_sources_from_dir(d)
    assert [s.name for s in srcs] == ["one", "two"]


def test_load_sources_duplicate_names_rejected(tmp_path, sync_mod):
    d = tmp_path / "sources"
    d.mkdir()
    (d / "a.yaml").write_text(
        "name: dup\nurl: u\nsrc_path: s\ndest_path: d\n"
    )
    (d / "b.yaml").write_text(
        "name: dup\nurl: u\nsrc_path: s\ndest_path: d2\n"
    )
    with pytest.raises(ValueError, match="duplicate source name"):
        sync_mod.load_sources_from_dir(d)


def test_load_sources_ignores_non_yaml_and_hidden(tmp_path, sync_mod):
    d = tmp_path / "sources"
    d.mkdir()
    (d / "a.yaml").write_text(
        "name: a\nurl: u\nsrc_path: s\ndest_path: d\n"
    )
    (d / "README.md").write_text("not a source")
    (d / ".hidden.yaml").write_text("name: hidden\n")  # should be skipped
    srcs = sync_mod.load_sources_from_dir(d)
    assert [s.name for s in srcs] == ["a"]


def test_load_sources_missing_dir_raises(tmp_path, sync_mod):
    with pytest.raises(FileNotFoundError):
        sync_mod.load_sources_from_dir(tmp_path / "does-not-exist")


def test_load_sources_bad_top_level_raises(tmp_path, sync_mod):
    d = tmp_path / "sources"
    d.mkdir()
    (d / "bad.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(ValueError, match="top-level must be a mapping"):
        sync_mod.load_sources_from_dir(d)


# ---------- _error_hash ----------


def test_error_hash_stable_across_volatile_bits(sync_mod):
    """Two errors differing only in path, SHA, timestamp, or line number
    must hash to the same value so we don't open a new issue each run."""
    a = (
        "Traceback at /home/runner/work/foo/bar.py line 42: "
        "error 2025-04-22T19:30:15Z commit abc1234"
    )
    b = (
        "Traceback at /home/ubuntu/work/other/path.py line 999: "
        "error 2024-01-02T03:04:05Z commit deadbeef"
    )
    assert sync_mod._error_hash("src", a) == sync_mod._error_hash("src", b)


def test_error_hash_source_scoped(sync_mod):
    """Same error under different source names must hash differently so
    each source gets its own issue."""
    msg = "boom"
    assert sync_mod._error_hash("s1", msg) != sync_mod._error_hash("s2", msg)


def test_error_hash_genuinely_different_errors_differ(sync_mod):
    assert sync_mod._error_hash("s", "aaa") != sync_mod._error_hash("s", "bbb")


def test_error_hash_is_12_hex(sync_mod):
    h = sync_mod._error_hash("s", "x")
    assert len(h) == 12
    int(h, 16)  # raises if not hex


# ---------- _source_name_from_issue ----------


def test_source_name_from_issue_reads_label(sync_mod):
    prefix = sync_mod.SOURCE_LABEL_PREFIX
    issue = {"labels": [{"name": "other"}, {"name": f"{prefix}my-source"}]}
    assert sync_mod._source_name_from_issue(issue) == "my-source"


def test_source_name_from_issue_missing(sync_mod):
    issue = {"labels": [{"name": "unrelated"}]}
    assert sync_mod._source_name_from_issue(issue) is None


# ---------- validate_skill_tree / format_validation_failures ----------


def test_validate_skill_tree_missing_dir_is_empty(sync_mod, tmp_path):
    """A first run that hasn't imported anything yet has no dest tree.
    That's legitimate — not an error."""
    assert sync_mod.validate_skill_tree(tmp_path / "nope") == {}


def test_validate_skill_tree_no_skill_md_files(sync_mod, tmp_path):
    (tmp_path / "empty").mkdir()
    assert sync_mod.validate_skill_tree(tmp_path / "empty") == {}


def test_validate_skill_tree_detects_broken_skill(sync_mod, tmp_path):
    """A SKILL.md with missing frontmatter fields surfaces as a failure."""
    sk = tmp_path / "tree" / "broken"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: wrong_name\n---\n# body\n"
    )
    failures = sync_mod.validate_skill_tree(tmp_path / "tree")
    assert len(failures) == 1
    (skill_dir, errors) = next(iter(failures.items()))
    assert skill_dir.name == "broken"
    assert errors  # at least one error message


def test_format_validation_failures_mentions_each_skill(sync_mod):
    # The formatter renders skill paths relative to REPO_ROOT, so the
    # failure paths must sit under it (in production they always do
    # because dest_path lives inside the repo being synced into).
    root = sync_mod.REPO_ROOT
    failures = {
        root / "tree" / "sk1": ["err a", "err b"],
        root / "tree" / "sk2": ["err c"],
    }
    text = sync_mod.format_validation_failures(root / "tree", failures)
    assert "sk1/SKILL.md" in text
    assert "sk2/SKILL.md" in text
    assert "err a" in text and "err b" in text and "err c" in text


def test_source_from_dict_squash_defaults_true(sync_mod):
    src = sync_mod.Source.from_dict(
        {
            "name": "n",
            "url": "u",
            "src_path": "a",
            "dest_path": "b",
        }
    )
    assert src.squash is True, "squash must default to True for new configs"


def test_source_from_dict_squash_explicit_false(sync_mod):
    src = sync_mod.Source.from_dict(
        {
            "name": "n",
            "url": "u",
            "src_path": "a",
            "dest_path": "b",
            "squash": False,
        }
    )
    assert src.squash is False


def test_source_from_dict_squash_string_variants(sync_mod):
    for val, expected in [
        ("true", True),
        ("false", False),
        ("yes", True),
        ("no", False),
        ("1", True),
        ("0", False),
    ]:
        src = sync_mod.Source.from_dict(
            {
                "name": "n",
                "url": "u",
                "src_path": "a",
                "dest_path": "b",
                "squash": val,
            }
        )
        assert src.squash is expected, f"squash={val!r} parsed wrong"


def test_source_from_dict_squash_invalid_raises(sync_mod):
    import pytest as _pytest  # local import; file has pytest imported globally
    with _pytest.raises(ValueError, match="squash"):
        sync_mod.Source.from_dict(
            {
                "name": "n",
                "url": "u",
                "src_path": "a",
                "dest_path": "b",
                "squash": 42,
            }
        )
