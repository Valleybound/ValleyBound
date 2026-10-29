#!/usr/bin/env python3
"""Unit tests for collect_market.py's deduplication and writing rules.

The collector must:
  - write a new file when the market content changed,
  - write nothing when a newer observation carries identical content,
  - replace a file when the same epoch is re-collected with corrected
    content.

Run with: python3 -m unittest discover tests -v
"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "collector"))

import collect_market as collector  # noqa: E402


def listing(item_id, price, quantity, last_seen):
    return {
        "item_id": item_id,
        "price": price,
        "quantity": quantity,
        "last_seen": last_seen,
    }


class CollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        collector.DATA_DIR = os.path.join(self.tmp.name, "data")
        collector.MANIFEST_PATH = os.path.join(collector.DATA_DIR,
                                               "manifest.json")
        self.listings = []
        collector.get = lambda url: self.fetch(url)

    def tearDown(self):
        self.tmp.cleanup()

    def serve(self, collected, price, item_id="item_a"):
        """One observation: a single listing whose price the caller
        controls, seen on every picked market."""
        self.listings = [listing(item_id, price, 10, collected)]

    def fetch(self, url):
        if url == collector.INDEX_URL:
            return [
                "https://data-cdn.gaming.tools/paxdei/market/%s.json" % pick
                for pick in collector.PICKS
            ]
        if url == collector.ITEMS_URL:
            return {"item_a": {"name": {"En": "Item A"}},
                    "item_b": {"name": {"En": "Item B"}}}
        # A zone fetch: every picked market serves the same observation.
        return list(self.listings)

    def data_files(self):
        if not os.path.isdir(collector.DATA_DIR):
            return []
        return sorted(
            name for name in os.listdir(collector.DATA_DIR)
            if name.endswith(".json") and name != "manifest.json")

    def test_new_content_writes_a_new_file(self):
        # Recent epochs, so the retention prune never touches them.
        epoch = int(time.time()) - 60
        self.serve(epoch, 50)
        self.assertEqual(collector.main(), 0)
        self.assertEqual(self.data_files(), ["%d.json" % epoch])

    def test_identical_content_under_a_newer_epoch_writes_nothing(self):
        epoch = int(time.time()) - 60
        self.serve(epoch, 50)
        self.assertEqual(collector.main(), 0)
        manifest_before = open(collector.MANIFEST_PATH, "rb").read()
        # A newer observation, but the market did not move: only the
        # collection timestamps inside the feed are newer.
        self.serve(epoch + 1000, 50)
        self.assertEqual(collector.main(), 0)
        self.assertEqual(self.data_files(), ["%d.json" % epoch])
        self.assertEqual(
            open(collector.MANIFEST_PATH, "rb").read(), manifest_before,
            "an unchanged market must not rewrite the manifest")

    def test_changed_content_writes_a_new_file(self):
        epoch = int(time.time()) - 60
        self.serve(epoch, 50)
        self.assertEqual(collector.main(), 0)
        self.serve(epoch + 1000, 60)
        self.assertEqual(collector.main(), 0)
        self.assertEqual(self.data_files(),
                         ["%d.json" % epoch, "%d.json" % (epoch + 1000)])

    def test_corrected_content_under_the_same_epoch_replaces(self):
        epoch = int(time.time()) - 60
        self.serve(epoch, 50)
        self.assertEqual(collector.main(), 0)
        self.serve(epoch, 55)
        self.assertEqual(collector.main(), 0)
        self.assertEqual(self.data_files(), ["%d.json" % epoch])
        kept = json.load(open(os.path.join(collector.DATA_DIR,
                                          "%d.json" % epoch)))
        prices = [rec["l"]
                  for market in kept["markets"]
                  for rec in market["items"].values()]
        self.assertTrue(prices and all(price == 55 for price in prices))


if __name__ == "__main__":
    unittest.main()
