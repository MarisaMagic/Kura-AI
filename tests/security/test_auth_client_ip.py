"""登录限流客户端 IP：信任代理时优先 X-Real-IP，XFF 取最后一跳。"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.utils.auth_rate_limit import client_ip


def _req(*, headers: dict, client_host: str = "10.0.0.8"):
    return SimpleNamespace(headers=headers, client=SimpleNamespace(host=client_host))


class ClientIpTests(unittest.TestCase):
    def test_untrusted_ignores_xff(self):
        req = _req(headers={"X-Forwarded-For": "1.2.3.4, 10.0.0.1"})
        with mock.patch("app.utils.auth_rate_limit.settings") as st:
            st.AUTH_TRUST_X_FORWARDED_FOR = False
            self.assertEqual(client_ip(req), "10.0.0.8")

    def test_trusted_prefers_x_real_ip(self):
        req = _req(
            headers={
                "X-Real-IP": "203.0.113.9",
                "X-Forwarded-For": "1.2.3.4, 203.0.113.9",
            }
        )
        with mock.patch("app.utils.auth_rate_limit.settings") as st:
            st.AUTH_TRUST_X_FORWARDED_FOR = True
            self.assertEqual(client_ip(req), "203.0.113.9")

    def test_trusted_xff_uses_last_hop(self):
        req = _req(headers={"X-Forwarded-For": "1.2.3.4, 198.51.100.10"})
        with mock.patch("app.utils.auth_rate_limit.settings") as st:
            st.AUTH_TRUST_X_FORWARDED_FOR = True
            self.assertEqual(client_ip(req), "198.51.100.10")


if __name__ == "__main__":
    unittest.main()
