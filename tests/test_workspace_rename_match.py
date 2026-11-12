import pytest

from plane_toolchain import workspace_rename_match as m

HOSTS = ["plane.example.com"]
RESTRICTED = {"api", "spaces"}


def test_validate_slug_accepts_regex_and_48_chars():
    for ok in ("a", "Acme_2-x", "a" * 48):
        m.validate_slug(ok, RESTRICTED, set())


@pytest.mark.parametrize("bad", ["", "a" * 49, "a b", "a/b", "a.b", "é", "a\n", None])
def test_validate_slug_rejects_49_chars_bad_chars_empty(bad):
    with pytest.raises(ValueError):
        m.validate_slug(bad, RESTRICTED, set())


def test_validate_slug_rejects_restricted():
    with pytest.raises(ValueError, match="restricted"):
        m.validate_slug("api", RESTRICTED, set())


def test_validate_slug_rejects_taken_including_soft_deleted_suffix():
    taken = {"acme", "gone__1700000000"}
    with pytest.raises(ValueError, match="taken"):
        m.validate_slug("acme", RESTRICTED, taken)
    with pytest.raises(ValueError, match="taken"):
        m.validate_slug("gone__1700000000", RESTRICTED, taken)
    # `gone__...` occupies only its own exact form
    m.validate_slug("gone", RESTRICTED, taken)


def test_rewrite_absolute_url_own_host():
    src = "see https://plane.example.com/acme/browse/X-1/ and http://PLANE.example.com/acme"
    out, n = m.rewrite_html(src, "acme", "beta", HOSTS)
    assert out == "see https://plane.example.com/beta/browse/X-1/ and http://PLANE.example.com/beta"
    assert n == 2


def test_absolute_url_port_aware():
    hosts = ["plane.example.com:8080"]
    out, n = m.rewrite_html("https://plane.example.com:8080/acme/x", "acme", "beta", hosts)
    assert (out, n) == ("https://plane.example.com:8080/beta/x", 1)
    assert m.rewrite_html("https://plane.example.com/acme/x", "acme", "beta", hosts)[1] == 0


def test_rewrite_root_relative_href():
    src = "<a href=\"/acme/x\">a</a><a href='/acme?q=1'>b</a>"
    out, n = m.rewrite_html(src, "acme", "beta", HOSTS)
    assert out == "<a href=\"/beta/x\">a</a><a href='/beta?q=1'>b</a>"
    assert n == 2


def test_rewrite_embed_workspace_identifier():
    src = '<div workspace_identifier="acme" project_identifier="P"></div>'
    out, n = m.rewrite_html(src, "acme", "beta", HOSTS)
    assert out == '<div workspace_identifier="beta" project_identifier="P"></div>'
    assert n == 1


def test_rewrite_terminators_slash_quotes_query_hash_end():
    for tail in ("/x", '"', "'", "?a=1", "#h", ""):
        src = f"https://plane.example.com/acme{tail}"
        out, n = m.rewrite_html(src, "acme", "beta", HOSTS)
        assert out == f"https://plane.example.com/beta{tail}", tail
        assert n == 1


def test_prefix_collision_untouched():
    src = (
        '<a href="/acme-two/x">x</a> <a href="/acme2">y</a> '
        'https://plane.example.com/acme-two/x <i workspace_identifier="acme-two"></i>'
    )
    assert m.rewrite_html(src, "acme", "beta", HOSTS) == (src, 0)


def test_other_host_untouched():
    src = "https://evil.example.org/acme/x https://plane.example.com.evil.io/acme/x"
    assert m.rewrite_html(src, "acme", "beta", HOSTS) == (src, 0)


def test_bare_word_untouched():
    src = "<p>Welcome to acme. Visit /acme or acme/x</p>"
    assert m.rewrite_html(src, "acme", "beta", HOSTS) == (src, 0)


def test_other_workspace_href_untouched():
    src = '<a href="/other/acme/x">x</a> <a href="/other">y</a>'
    assert m.rewrite_html(src, "acme", "beta", HOSTS) == (src, 0)


def test_json_key_workspace_slug_exact_value_only():
    obj = {"workspace_slug": "acme", "other": "acme", "x": {"workspace_slug": "acme-two"}}
    out, n = m.rewrite_json_slug(obj, "acme", "beta", HOSTS)
    assert out == {"workspace_slug": "beta", "other": "acme", "x": {"workspace_slug": "acme-two"}}
    assert n == 1
    assert obj["workspace_slug"] == "acme"  # input not mutated


def test_json_nested_and_lists():
    obj = {"a": [{"workspace_slug": "acme"}, [{"workspace_slug": "acme"}], 3, None]}
    out, n = m.rewrite_json_slug(obj, "acme", "beta", HOSTS)
    assert out == {"a": [{"workspace_slug": "beta"}, [{"workspace_slug": "beta"}], 3, None]}
    assert n == 2


def test_scan_text_counts_forms_without_rewriting():
    src = (
        'https://plane.example.com/acme/x <a href="/acme/y">y</a> '
        '<i workspace_identifier="acme"></i> <a href="/acme-two">z</a>'
    )
    assert m.scan_text(src, "acme", HOSTS) == {"url": 1, "href": 1, "embed": 1}
    assert m.scan_text("nothing", "acme", HOSTS) == {"url": 0, "href": 0, "embed": 0}


def test_rewrite_is_idempotent_and_new_not_matched_when_old_is_prefix_of_new():
    src = (
        'https://plane.example.com/acme/x <a href="/acme/y"></a> '
        '<i workspace_identifier="acme"></i>'
    )
    once, n = m.rewrite_html(src, "acme", "acme-two", HOSTS)
    assert n == 3
    assert m.rewrite_html(once, "acme", "acme-two", HOSTS) == (once, 0)
    assert "acme-two/x" in once and "acme-two-two" not in once


DOC = {
    "type": "doc",
    "content": [
        {"type": "issue-embed", "attrs": {"workspace_identifier": "acme", "id": "1"}},
        {
            "type": "text",
            "text": "acme",
            "marks": [{"type": "link", "attrs": {"href": "https://plane.example.com/acme/x"}}],
        },
        {"type": "text", "text": "r", "marks": [{"type": "link", "attrs": {"href": "/acme?a=1"}}]},
    ],
}


def test_json_rewrites_embed_workspace_identifier():
    out, n = m.rewrite_json_slug(DOC, "acme", "beta", HOSTS)
    assert out["content"][0]["attrs"] == {"workspace_identifier": "beta", "id": "1"}
    assert n == 3
    assert DOC["content"][0]["attrs"]["workspace_identifier"] == "acme"


def test_json_rewrites_link_href_own_host_and_root_relative():
    out, _ = m.rewrite_json_slug(DOC, "acme", "beta", HOSTS)
    assert out["content"][1]["marks"][0]["attrs"]["href"] == "https://plane.example.com/beta/x"
    assert out["content"][2]["marks"][0]["attrs"]["href"] == "/beta?a=1"
    assert out["content"][1]["text"] == "acme"


def test_json_leaves_other_host_href_and_prefix_collision():
    obj = {
        "a": {"href": "https://evil.example.org/acme/x"},
        "b": {"href": "/acme-two/x"},
        "c": {"href": "https://plane.example.com/acme2"},
        "d": {"workspace_identifier": "acme-two"},
    }
    assert m.rewrite_json_slug(obj, "acme", "beta", HOSTS) == (obj, 0)


def test_json_leaves_unrelated_string_values():
    obj = {"text": "see /acme/x and https://plane.example.com/acme", "name": "acme", "x": ["acme"]}
    assert m.rewrite_json_slug(obj, "acme", "beta", HOSTS) == (obj, 0)


def test_scan_json_counts_forms_without_rewriting():
    assert m.scan_json(DOC, "acme", HOSTS) == {"url": 1, "href": 1, "embed": 1, "slug": 0}
    assert m.scan_json({"workspace_slug": "acme"}, "acme", HOSTS)["slug"] == 1
