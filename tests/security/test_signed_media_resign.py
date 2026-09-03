"""媒体重签：无有效签名的路径不得换发新签。"""

from __future__ import annotations

import unittest
from unittest import mock

from app.utils.signed_media import KIND_USER_AVATAR, resign_media_url, sign_media_url


class ResignMediaTests(unittest.TestCase):
    def test_unsigned_path_not_resigned(self):
        raw = "/api/v1/media/user_avatar/someone.png"
        with mock.patch("app.utils.signed_media.settings") as st:
            st.SECRET_KEY = "test-secret-key-for-hmac"
            st.MEDIA_SIGNED_URL_TTL_SECONDS = 3600
            st.PUBLIC_API_BASE = ""
            self.assertEqual(resign_media_url(raw), raw)

    def test_valid_signed_url_is_refreshed(self):
        with mock.patch("app.utils.signed_media.settings") as st:
            st.SECRET_KEY = "test-secret-key-for-hmac"
            st.MEDIA_SIGNED_URL_TTL_SECONDS = 3600
            st.PUBLIC_API_BASE = ""
            signed = sign_media_url(KIND_USER_AVATAR, "alice.png")
            out = resign_media_url(signed)
            self.assertIn("/api/v1/media/user_avatar/alice.png", out)
            self.assertIn("sig=", out)
            self.assertNotEqual(out, signed)

    def test_forged_sig_not_resigned(self):
        raw = "/api/v1/media/user_avatar/alice.png?exp=9999999999&sig=deadbeef"
        with mock.patch("app.utils.signed_media.settings") as st:
            st.SECRET_KEY = "test-secret-key-for-hmac"
            st.MEDIA_SIGNED_URL_TTL_SECONDS = 3600
            st.PUBLIC_API_BASE = ""
            self.assertEqual(resign_media_url(raw), raw)


if __name__ == "__main__":
    unittest.main()
