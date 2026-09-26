from postcard.core.mime.html_colors import recolor


def test_page_colors_give_way_to_the_reader_surface() -> None:
    body = '<body bgcolor="white"><div style="background-color:#fff">x</div></body>'
    for is_dark in (False, True):
        out = recolor(body, is_dark=is_dark)
        assert 'bgcolor="transparent"' in out
        assert "background-color:transparent" in out


def test_black_pages_give_way_only_in_dark_mode() -> None:
    body = '<table style="background:#000"><tr></tr></table>'
    assert "background:transparent" in recolor(body, is_dark=True)
    assert recolor(body, is_dark=False) == body


def test_dark_mode_mirrors_light_fills_and_dark_text() -> None:
    out = recolor('<p style="color:#000;background:#e8f0fe">x</p>', is_dark=True)
    assert "color:#ffffff" in out
    assert "#e8f0fe" not in out
    assert recolor('<font color="#333333">x</font>', is_dark=True) == (
        '<font color="#cccccc">x</font>'
    )


def test_light_mode_keeps_everything_but_the_page() -> None:
    body = '<p style="color:#000;background:#e8f0fe">x</p>'
    assert recolor(body, is_dark=False) == body


def test_mid_tones_and_other_properties_are_left_alone() -> None:
    body = '<a style="color:#3584e4;border-color:#000;background:#26a269">x</a>'
    assert recolor(body, is_dark=True) == body


def test_style_blocks_are_rewritten_and_text_is_not() -> None:
    body = "<style>p{color:black}@media screen{.x{background:white}}</style>"
    body += "<p>set color: black on it</p>"
    out = recolor(body, is_dark=True)
    assert "p{color:#ffffff}" in out
    assert ".x{background:transparent}" in out
    assert "<p>set color: black on it</p>" in out


def test_urls_and_important_survive() -> None:
    body = '<div style="background:url(img/white.png) #fff !important">x</div>'
    assert recolor(body, is_dark=True) == (
        '<div style="background:url(img/white.png) transparent !important">x</div>'
    )


def test_translucent_colors_keep_their_alpha() -> None:
    out = recolor('<p style="color:rgba(0, 0, 0, 0.5)">x</p>', is_dark=True)
    assert "color:rgba(255,255,255,0.50)" in out
