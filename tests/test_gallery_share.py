"""The customer gallery's Share button (``render_gallery_html(share_url=…)``).

A customer reported they could not find any way to share their gallery from the
``/j/{code}`` page. The served route now passes the gallery's own CLEAN link and the
hero renders one Share control:

* a ``<details>`` menu — WhatsApp / Facebook / Email anchors and the link in a
  read-only box — so it works with no JavaScript at all;
* a script that opens the device's native share sheet where one exists, and turns the
  box into a Copy-link button where the clipboard is available;
* ``og:`` title/description so a pasted link previews as the customer's gallery.

What it must never do: put the ``?s=`` source tag or a ``#tab`` fragment in the shared
link, leak the link through a Referer, or appear on the legacy S3 page (whose
presigned URLs expire).
"""

from __future__ import annotations

import re
import urllib.parse

from fastapi.testclient import TestClient

from api.gallery import render_gallery_html

from .test_addon_crosssell import _job_with, client, queue  # noqa: F401  (fixtures)

SHARE = "https://gallery.test/j/AbC123"


def _page(**kw: object) -> str:
    base: dict[str, object] = dict(
        brand="Parachute Montréal",
        customer_name="Ada Byron",
        jump_date="2026-08-14",
        location=None,
        videos=[("full_video", "/j/AbC123/v/full_video.mp4")],
        photos=[],
        tabbed=True,
        show_downloads=True,
        poll_token="AbC123",
    )
    base.update(kw)
    return render_gallery_html(**base)  # type: ignore[arg-type]


def _hrefs(html: str) -> dict[str, str]:
    return {
        m.group(1): urllib.parse.unquote(m.group(2).replace("&amp;", "&"))
        for m in re.finditer(r'data-share-to="(\w+)"[^>]*href="([^"]+)"', html)
    }


def test_served_page_has_one_share_control_in_the_hero() -> None:
    html = _page(share_url=SHARE)
    assert html.count("data-share ") == 1
    hero = html.split('<div class="hero">', 1)[1].split("</div></details></div>", 1)[0]
    assert "<summary" in hero and "Share" in hero
    # Rendered on the page, not hidden behind a script: the menu is a <details>.
    assert '<details><summary class="sharebtn">' in html


def test_every_target_carries_the_clean_link_and_a_sentence() -> None:
    h = _hrefs(_page(share_url=SHARE))
    assert set(h) == {"whatsapp", "facebook", "email"}
    sentence = "Check out my skydive at Parachute Montréal!"
    assert h["whatsapp"] == f"https://wa.me/?text={sentence} {SHARE}"
    assert h["facebook"] == f"https://www.facebook.com/sharer/sharer.php?u={SHARE}"
    assert h["email"].startswith("mailto:?subject=Ada Byron — Parachute Montréal&body=")
    assert h["email"].endswith(SHARE)


def test_the_link_box_works_without_javascript() -> None:
    html = _page(share_url=SHARE)
    assert f'data-share-box readonly value="{SHARE}"' in html
    # The copy button starts hidden; only the script reveals it (and hides the box)
    # once it knows the clipboard is available.
    assert "data-share-copy hidden" in html


def test_outbound_share_links_never_send_a_referer() -> None:
    """The gallery URL is the customer's credential (see the page's referrer meta)."""
    html = _page(share_url=SHARE)
    for m in re.finditer(r'<a class="shareitem"[^>]*>', html):
        assert 'rel="noreferrer' in m.group(0)
    assert '<meta name="referrer" content="no-referrer">' in html


def test_native_sheet_runs_from_the_tap_and_a_dismissal_is_silent() -> None:
    html = _page(share_url=SHARE)
    assert "if(navigator.share){sm.addEventListener('click'" in html
    assert "navigator.share({title:t,text:x,url:u})" in html
    assert "er.name==='AbortError')return" in html
    # A relative link is resolved against the page, so the shared URL is absolute.
    assert "new URL(w.getAttribute('data-url'),location.href).href" in html


def test_link_preview_tags_describe_the_gallery() -> None:
    html = _page(share_url=SHARE)
    assert '<meta property="og:title" content="Ada Byron — Parachute Montréal">' in html
    desc = "Check out my skydive at Parachute Montréal!"
    assert f'<meta property="og:description" content="{desc}">' in html
    assert f'<meta property="og:url" content="{SHARE}">' in html


def test_names_are_escaped_in_the_share_attributes() -> None:
    html = _page(share_url=SHARE, customer_name='Bobby "Tables" <b>')
    assert 'data-title="Bobby &quot;Tables&quot; &lt;b&gt; — Parachute Montréal"' in html
    assert "<b>" not in html.split('data-title="', 1)[1].split('"', 1)[0]


def test_a_locked_page_is_shareable_too() -> None:
    """A friend opening it lands on the watermarked preview and its unlock offer."""
    html = _page(
        share_url=SHARE, locked=True, show_downloads=False,
        unlock_url="https://dz.test/unlock/j1",
    )
    assert "data-share " in html
    assert "Unlock full video" in html


def test_without_a_share_url_nothing_is_rendered() -> None:
    """The legacy S3 page passes none: no control, no tags, no extra script."""
    html = _page()
    assert "data-share" not in html
    assert "og:" not in html
    assert "navigator.share" not in html
    assert html.count("<script>") == _page(share_url=SHARE).count("<script>") - 1


def test_the_served_route_shares_the_clean_link(client: TestClient) -> None:  # noqa: F811
    """Opened from the email (``?s=e``) on the Video tab, the page still shares
    ``{PUBLIC_BASE_URL}/j/{code}`` — the source tag is analytics for the ORIGINAL
    send and must not travel with every forward."""
    _, token = _job_with(client, "video_only", videos=True, photos=False)
    r = client.get(f"/j/{token}?s=e")
    assert r.status_code == 200
    html = r.text
    clean = f"https://gallery.test/j/{token}"
    assert f'data-url="{clean}"' in html
    assert f'<meta property="og:url" content="{clean}">' in html
    assert "s=e" not in html.split("data-share ", 1)[1].split("</details>", 1)[0]
    assert _hrefs(html)["facebook"] == f"https://www.facebook.com/sharer/sharer.php?u={clean}"
