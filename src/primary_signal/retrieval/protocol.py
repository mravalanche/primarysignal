"""Shared identifiers for the isolated retriever's readiness contract."""

READY_PATH = "/v1/ready"
PROTOCOL_VERSION = 1
FEED_FETCH_CAPABILITY = "feeds.fetch"
ARTICLE_FETCH_CAPABILITY = "articles.fetch"
CAPABILITIES = (FEED_FETCH_CAPABILITY, ARTICLE_FETCH_CAPABILITY)
