"""Portable external-origin fixtures without requiring a second loopback address."""

from urllib.parse import urlsplit

from open_verify.tools import LocalTools


def external_destination(monkeypatch, url):
    """Classify one fixture port as external while exercising the real network guards."""
    authority = urlsplit(url).netloc
    original = LocalTools.check_url

    def check_url(self, candidate):
        if urlsplit(candidate).netloc == authority and self.origin(candidate) not in self.origins:
            raise ValueError("External origin requires --allow-origin: " + self.origin(candidate))
        return original(self, candidate)

    monkeypatch.setattr(LocalTools, "check_url", check_url)
