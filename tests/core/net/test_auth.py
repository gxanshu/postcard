from postcard.core.net.auth import xoauth2_response


def test_xoauth2_response_uses_the_control_character_separators():
    # The \x01 bytes are the format (RFC 7628), not whitespace -- a server
    # rejects the whole exchange if they are spaces.
    assert (
        xoauth2_response("ada@example.com", "ya29.token")
        == "user=ada@example.com\x01auth=Bearer ya29.token\x01\x01"
    )
